"""Run one Code Review from start to end. See docs/spec/05-review-engine.md."""

import time
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe.utils import now_datetime

from guard import settings as guard_settings
from guard.config import gate
from guard.config.resolver import ConfigResolver
from guard.git.diff import parse_diff
from guard.git.repository import store_for
from guard.git.store import GitStore
from guard.graph.overlay import PullRequestGraph
from guard.graph.queries import CodeGraph
from guard.indexing import frappe_plugin
from guard.indexing.indexer import IndexBusy
from guard.indexing.jobs import ensure_fresh
from guard.integrations import tickets
from guard.learning import suppression
from guard.llm.types import GatewayBusy
from guard.providers import adapter_for_repository
from guard.review import checks, context, crossrepo, prompts, rule_files, sandbox, stale, verify
from guard.review.agent import Agent
from guard.review.findings import FindingFilter, score_cap, score_floor
from guard.review.publish import Publisher, set_status
from guard.review.run import ReviewRun, Verdict
from guard.review.task import compose
from guard.utils import db
from guard.utils.redact import redact

if TYPE_CHECKING:
	from guard.guard.doctype.code_review.code_review import CodeReview
	from guard.guard.doctype.pull_request.pull_request import PullRequest

AREA_FILES = 8
RETRY_DELAYS = (60, 300, 900)


class Cancelled(Exception):
	pass


class ReviewPipeline:
	def __init__(self, review: str):
		self.review = cast("CodeReview", frappe.get_doc("Code Review", review))
		self.started = time.time()
		self.publisher: Publisher | None = None  # set once the Guard check shows the review

	@classmethod
	def run(cls, review: str, attempt: int = 0) -> None:
		pipeline = cls(review)
		try:
			pipeline.attempt(attempt)
		finally:
			pipeline.acknowledge()

	def acknowledge(self) -> None:
		"""Finish the mention that asked for this review: 👍 when it completed, 😕 when it did not."""
		status = db.value("Code Review", str(self.review.name), "status")
		if status in ("Queued", "Running") or not self.review.pull_request or not self.review.requested_by:
			return
		from guard.conversation import acks

		acks.review_finished(
			str(self.review.pull_request), str(self.review.requested_by), failed=status != "Completed"
		)

	def attempt(self, attempt: int) -> None:
		try:
			self.execute()
		except Cancelled:
			self.review.db_set({"status": "Cancelled", "completed_at": now_datetime()})
			self.close_check()
		except GatewayBusy as busy:
			self.retry(attempt, busy.retry_after, "the AI gateway", f"AI gateway busy: {busy}")
		except IndexBusy:
			# Do not index the branch a second time, in parallel.
			self.retry(attempt, 0, "the code graph index", "The code graph index stayed busy.")
		except Exception as error:
			db.rollback()
			self.fail(redact(f"{type(error).__name__}: {error}"))
			if frappe.flags.in_test:
				raise

	def retry(self, attempt: int, after: int, waiting_for: str, failure: str) -> None:
		delay = max(after, RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
		if attempt >= len(RETRY_DELAYS):
			self.fail(failure)
			return
		self.review.db_set({"status": "Queued", "stage": f"waiting {delay}s for {waiting_for}"})
		db.commit()
		from guard.review.jobs import enqueue_review

		enqueue_review(str(self.review.name), delay=delay, attempt=attempt + 1)

	# ---------------------------------------------------------------- stages

	def execute(self) -> None:
		run = self.prepare()
		if run is None:
			return
		publisher = Publisher(run)
		self.stage("start")
		publisher.start()
		self.publisher = publisher
		self.stage("index")
		index = ensure_fresh(run.repository) if run.repository else None
		run.graph = CodeGraph(index) if index else None
		self.stage("context")
		overlay = PullRequestGraph(run.graph, run.head, run.base, run.files, run.frappe_app)
		run.changed = overlay.build()
		others = self.linked_repositories(run)
		crossrepo.attach(run.changed, others, index)
		run.blast = overlay.blast_radius(run.changed)
		if run.settings.get("include_rule_files", True):
			run.rule_files = rule_files.collect(run.paths, run.head, run.list_dir)
		self.pick_tier(run)
		if not self.credits_allow(run):
			publisher.skip("the monthly credit cap is reached")
			return
		linked = (
			tickets.for_pull_request(run.title, run.description, run.head_branch)
			if run.pr is not None
			else []
		)
		run.context_used = [{"title": f"{t.key} {t.title}", "url": t.url, "source": t.source} for t in linked]
		team = db.value("Code Repository", run.repository, "team") if run.repository else None
		built = context.build(
			run.head,
			run.changed,
			run.blast,
			run.graph,
			run.cfg,
			run.paths,
			linked,
			run.full_name,
			team,
			run.repository,
			run.rule_files,
		)
		self.stage("review")
		run.findings += checks.secret_findings(run.files)
		run.findings += crossrepo.missing_targets(run.files, run.head, others, index)
		run.findings += crossrepo.break_findings(run.changed)
		run.findings += checks.missing_test_findings(
			run.files,
			[f.path for f in run.all_files if f.status != "deleted"],
			rule_files.mandatory_rules(run.rule_files),
		)
		if run.frappe_app:
			run.findings += checks.frappe_findings(run.files)
		run.sandbox = sandbox.Sandbox(run) if sandbox.wanted(run) else None
		try:
			self.agent(run, built)
		finally:
			if run.sandbox is not None:
				run.sandbox.cleanup()
		self.stage("verify")
		verify.verify(run)
		self.stage("filter")
		existing = self.existing_findings()
		finding_filter = FindingFilter(run.files, run.settings, existing, run.fresh_lines)
		finding_filter.run(run.findings)
		suppression.suppress(run.findings, run.repository)
		if not run.verdict.submitted:
			run.verdict = graph_verdict(run)
		score = score_floor(run.findings, run.verdict.confidence_score)
		run.verdict.confidence_score = min(score, score_cap(run.findings))
		self.stage("publish")
		names = publisher.save_findings()
		number = self.review_number()
		publisher.publish(names, number)
		# Close stale findings first: the status check counts the open ones.
		stale.close_stale(run, finding_filter.reraised)
		publisher.finish()
		self.stage("after")
		suppression.remember(names, run.findings, run.repository)
		self.after(run, number)

	def linked_repositories(self, run: ReviewRun) -> list[crossrepo.OtherRepository]:
		if not run.repository:
			return []
		provider = db.value("Code Repository", run.repository, "code_provider")
		return crossrepo.linked(run.repository, str(provider or ""), run.cfg.repos)

	def prepare(self) -> ReviewRun | None:
		self.review.db_set({"status": "Running", "started_at": now_datetime(), "stage": "prepare"})
		db.commit()
		if self.review.source == "cli":
			from guard.review.cli_review import prepare_cli

			return prepare_cli(self.review)
		pr = cast("PullRequest", frappe.get_doc("Pull Request", str(self.review.pull_request)))
		repository = str(pr.repository)
		number = int(pr.number)
		full_name = str(db.value("Code Repository", repository, "full_name"))
		adapter = adapter_for_repository(repository)
		store = store_for(repository)
		store.fetch_branches(pr.base_branch or "")
		if adapter and adapter.pull_ref(number):
			head = store.fetch_pull(adapter.pull_ref(number), number)
		else:
			head = store.fetch_branch(pr.head_branch or "", number, pr.head_clone_url or None)
		base_ref = f"refs/heads/{pr.base_branch}"
		merge_base = store.merge_base(store.rev_parse(base_ref), head)
		files = parse_diff(store.diff(merge_base, head))
		resolver = ConfigResolver.from_store(repository, store, head)
		all_paths = [f.path for f in files]
		config = resolver.resolve_pr(all_paths)
		reviewed = set(config.reviewed_paths())
		run = ReviewRun(
			review=self.review,
			repository=repository,
			full_name=full_name,
			head=lambda path: store.show(head, path),
			base=lambda path: store.show(merge_base, path),
			list_dir=lambda path: store.list_dir(head, path),
			head_sha=head,
			base_sha=merge_base,
			title=pr.title or "",
			description=pr.description or "",
			author=pr.author or "",
			labels=list(frappe.parse_json(pr.labels or "[]") or []),
			base_branch=pr.base_branch or "",
			head_branch=pr.head_branch or "",
			pr=pr,
			adapter=adapter,
			all_files=files,
			files=[f for f in files if f.path in reviewed and not f.is_binary],
			config=config,
			commits=store.commits_between(merge_base, head),
			instructions=self.review.instructions or "",
			frappe_app=frappe_plugin.is_frappe_app({e.path for e in store.ls_tree(head)}),
		)
		run.notes = list(config.errors)
		run.fresh_lines = self.get_fresh_lines(store, pr.last_reviewed_sha or "", head)
		self.review.db_set(
			{
				"head_sha": head,
				"merge_base_sha": merge_base,
				"base_sha": pr.base_sha,
				"strictness": config.settings["strictness"],
				"changed_files": frappe.as_json([{"path": f.path, "status": f.status} for f in files]),
			}
		)
		if not run.files:
			self.review.db_set(
				{"status": "Skipped", "error": "no reviewable files", "completed_at": now_datetime()}
			)
			Publisher(run).skip("no reviewable files")
			return None
		return run

	def pick_tier(self, run: ReviewRun) -> None:
		facts = gate.PullRequestFacts(
			repository=run.full_name,
			title=run.title,
			description=run.description,
			author=run.author,
			base_branch=run.base_branch,
			head_branch=run.head_branch,
			labels=run.labels,
			paths=[p for f in run.files for p in {f.path, f.old_path} if p],
			additions=sum(f.additions for f in run.files),
			deletions=sum(f.deletions for f in run.files),
		)
		from guard.guard.doctype.review_settings.review_settings import get_effective

		team = db.value("Code Repository", run.repository, "team") if run.repository else None
		rules = gate.dashboard_rules(get_effective(team))
		tier_rules = [r.review_tier for r in rules if r.review_tier and gate.rule_matches(r, facts)]
		contracts = sum(1 for e in run.changed if e.contract_changed)
		run.tier = gate.select_tier(
			run.cfg,
			tier_rules,
			self.review.requested_tier or None,
			facts,
			contracts,
			first_review=self.review_number() == 1,
		)
		self.review.db_set("tier", run.tier)

	def credits_allow(self, run: ReviewRun) -> bool:
		cap = int(guard_settings.settings().monthly_credit_cap or 0)
		if not cap:
			return True
		used = int(
			db.scalar(
				"SELECT IFNULL(SUM(credits), 0) FROM `tabCode Review` WHERE creation >= DATE_FORMAT(NOW(), '%%Y-%%m-01') AND status = 'Completed'"
			)
			or 0
		)
		if used + guard_settings.CREDITS[run.tier] <= cap:
			return True
		self.review.db_set(
			{"status": "Skipped", "error": "monthly credit cap reached", "completed_at": now_datetime()}
		)
		if run.adapter and run.pr is not None:
			run.adapter.post_comment(
				run.full_name,
				int(run.pr.number),
				"Guard skipped this review: the monthly credit cap is reached.",
			)
		return False

	def agent(self, run: ReviewRun, built: context.BuiltContext) -> None:
		from guard.guard.doctype.review_settings.review_settings import get_effective

		team = db.value("Code Repository", run.repository, "team") if run.repository else None
		risk = str(get_effective(team).get("risk_definitions") or "")
		system = prompts.system_prompt(int(run.settings.get("strictness") or 2), "", risk)
		turns = guard_settings.max_turns(run.tier)
		if run.tier != "apex" or len(run.files) <= AREA_FILES:
			Agent(run, system, turns).run_loop(compose(run, built))
			return
		all_files = run.files
		for start in range(0, len(all_files), AREA_FILES):
			self.check_cancel()
			run.files = all_files[start : start + AREA_FILES]
			Agent(run, system, turns).run_loop(
				compose(
					run,
					built,
					f"Review only the files in this diff part ({start // AREA_FILES + 1}). Submit a verdict for this part.",
				)
			)
		run.files = all_files
		reported = "\n".join(f"- {f.path}:{f.start_line} {f.title}" for f in run.findings)
		Agent(run, system, turns).run_loop(
			compose(run, built, prompts.CROSS_CUTTING + "\n\nAlready reported:\n" + reported)
		)

	def after(self, run: ReviewRun, number: int) -> None:
		posted = [f for f in run.findings if f.kept]
		self.review.db_set(
			{
				"status": "Completed",
				"completed_at": now_datetime(),
				"duration": round(time.time() - self.started, 1),
				"summary": run.verdict.summary,
				"confidence_score": run.verdict.confidence_score,
				"confidence_reason": run.verdict.confidence_reason,
				"risk_level": run.verdict.risk_level,
				"risk_reason": run.verdict.risk_reason,
				"diagram_type": "" if run.verdict.diagram_type == "none" else run.verdict.diagram_type,
				"diagram": run.verdict.diagram,
				"important_files": frappe.as_json(run.verdict.important_files),
				"context_used": frappe.as_json(run.context_used),
				"config_snapshot": frappe.as_json(run.settings),
				"files_reviewed": len(run.files),
				"findings_count": len(run.findings),
				"posted_count": len(posted),
				"suppressed_count": sum(1 for f in run.findings if f.status == "Suppressed"),
				"credits": guard_settings.CREDITS.get(run.tier, 1),
				"input_tokens": run.usage.input_tokens,
				"output_tokens": run.usage.output_tokens,
				"review_number": number,
			}
		)
		if run.pr is not None:
			run.pr.db_set(
				{
					"review_count": number,
					"last_reviewed_sha": run.head_sha,
					"latest_review": self.review.name,
					"confidence_score": run.verdict.confidence_score,
					"risk_level": run.verdict.risk_level,
				}
			)
			frappe.enqueue(
				"guard.review.jobs.after_review",
				queue="short",
				enqueue_after_commit=True,
				review=self.review.name,
				now=bool(frappe.flags.in_test),
			)
		db.commit()
		data = {"review": self.review.name, "pull_request": self.review.pull_request}
		# Document rooms: only people who have the review or the pull request open get the event.
		frappe.publish_realtime("guard_review_updated", data, doctype="Code Review", docname=self.review.name)
		if self.review.pull_request:
			frappe.publish_realtime(
				"guard_review_updated", data, doctype="Pull Request", docname=self.review.pull_request
			)

	# --------------------------------------------------------------- helpers

	def stage(self, name: str) -> None:
		self.check_cancel()
		self.review.db_set("stage", name, update_modified=False)
		db.commit()

	def close_check(self) -> None:
		"""Stop the Guard check showing this review, unless a newer review of the same commit owns it."""
		newer = frappe.db.exists(
			"Code Review",
			{
				"pull_request": self.review.pull_request,
				"head_sha": self.review.head_sha,
				"status": ["in", ["Queued", "Running"]],
				"name": ["!=", self.review.name],
			},
		)
		if self.publisher and not newer:
			self.publisher.cancel()

	def check_cancel(self) -> None:
		if db.value("Code Review", str(self.review.name), "cancel_requested"):
			raise Cancelled

	def existing_findings(self) -> list[dict[str, Any]]:
		if not self.review.pull_request:
			return []
		return db.dict_rows(
			"""SELECT name, fingerprint, path, title FROM `tabReview Finding`
			WHERE pull_request = %s AND status = 'Open' AND posted = 1 AND code_review != %s""",
			(self.review.pull_request, self.review.name),
		)

	def get_fresh_lines(self, store: GitStore, last_sha: str, head: str) -> dict[str, set[int]] | None:
		"""Head lines added since the last reviewed commit. Empty when that commit is gone."""
		if self.review_number() <= 1:
			return None
		if not last_sha or not store.has_commit(last_sha):
			return {}
		return {f.path: f.added_lines() for f in parse_diff(store.diff(last_sha, head))}

	def review_number(self) -> int:
		if not self.review.pull_request:
			return 1
		return int(
			db.scalar(
				"SELECT COUNT(*) FROM `tabCode Review` WHERE pull_request = %s AND status IN ('Completed', 'Running')",
				(self.review.pull_request,),
			)
			or 1
		)

	def fail(self, message: str) -> None:
		self.review.db_set({"status": "Failed", "error": message[:5000], "completed_at": now_datetime()})
		try:
			self._report_failure()
		except Exception:
			frappe.log_error(redact(message), "Guard review")
		db.commit()

	def _report_failure(self) -> None:
		if not self.review.pull_request:
			return
		pr = cast("PullRequest", frappe.get_doc("Pull Request", str(self.review.pull_request)))
		adapter = adapter_for_repository(str(pr.repository))
		if not adapter:
			return
		full_name = str(db.value("Code Repository", str(pr.repository), "full_name"))
		# The status first: a failed reaction must not leave the check in progress.
		if self.review.head_sha:
			set_status(adapter, full_name, self.review.head_sha, "error", "Guard could not finish the review")
		adapter.add_reaction(full_name, int(pr.number), "pr", "", "confused")


def graph_verdict(run: ReviewRun) -> Verdict:
	"""A verdict from the graph alone, when the model gave none (no gateway, or no answer)."""
	contracts = [e for e in run.changed if e.contract_changed]
	lines = [
		f"Guard reviewed {len(run.files)} file(s) with the codebase graph. The AI review did not run, so no line comments came from a model."
	]
	if run.blast and run.blast.sentence():
		lines.append(run.blast.sentence())
	for entity in contracts[:10]:
		lines.append(
			f"- `{entity.qualified_name}` changed its contract; {len(entity.not_updated)} caller(s) may break."
		)
	risk = "high" if any(e.not_updated for e in contracts) else "medium" if contracts else "low"
	score = 3 if risk == "high" else 5
	return Verdict(
		summary="\n".join(lines),
		confidence_score=score,
		confidence_reason="Graph checks only.",
		risk_level=risk,
		submitted=True,
	)


def run_review(review: str, attempt: int = 0) -> None:
	"""Background job entry point."""
	ReviewPipeline.run(review, attempt)
