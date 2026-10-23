"""Write findings, the summary, the status and reactions to GitHub and to the database."""

import frappe

from guard.learning import memories
from guard.providers.base import InlineComment, PostedComment, ProviderAdapter, ProviderError
from guard.review import links, render
from guard.review.findings import Finding
from guard.review.run import ReviewRun
from guard.utils import cache, db


class Publisher:
	def __init__(self, run: ReviewRun):
		self.run = run
		self.adapter = run.adapter
		self.repo = run.full_name
		self.number = int(run.pr.number) if run.pr is not None else 0

	@property
	def gh(self) -> ProviderAdapter:
		if self.adapter is None:
			raise ProviderError("The repository has no code provider")
		return self.adapter

	# ------------------------------------------------------------ records

	def save_findings(self) -> dict[int, str]:
		"""One Review Finding for every finding, kept or not. Returns id(finding) -> record name."""
		names: dict[int, str] = {}
		rules = self._rule_contexts()
		for finding in self.run.findings:
			if finding.rule_id and not finding.context_name:
				finding.context_name = rules.get(finding.rule_id) or memories.cited(finding.rule_id)
			doc = frappe.get_doc(
				{
					"doctype": "Review Finding",
					"code_review": self.run.review.name,
					"pull_request": self.run.pr.name if self.run.pr is not None else None,
					"repository": self.run.repository,
					"title": finding.title,
					"severity": finding.severity,
					"comment_type": finding.comment_type,
					"category": finding.category,
					"is_security": int(finding.is_security),
					"status": finding.status,
					"status_reason": finding.status_reason,
					"confidence": finding.confidence,
					"outside_diff": int(finding.outside_diff),
					"verified": int(finding.verified),
					"path": finding.path,
					"start_line": finding.start_line,
					"end_line": finding.end_line,
					"side": finding.side,
					"fingerprint": finding.fingerprint,
					"body": finding.body,
					"suggestion": finding.suggestion,
					"question": finding.question,
					"fix_prompt": render.fix_prompt(finding),
					"custom_context": finding.context_name,
					"author": self.run.author,
					"sandbox_log": finding.sandbox_log,
				}
			).insert(ignore_permissions=True)
			names[id(finding)] = str(doc.name)
		return names

	# ------------------------------------------------------------ github

	def _rule_contexts(self) -> dict[str, str]:
		"""rule_id -> Custom Context name, for dashboard rules in this review."""
		mapping: dict[str, str] = {}
		if self.run.config:
			for file_config in self.run.config.files.values():
				for rule in file_config.rules:
					if rule.id and rule.context_name:
						mapping[rule.id] = rule.context_name
		return mapping

	def publish(self, names: dict[int, str], review_number: int) -> None:
		if not self.adapter or self.run.pr is None:
			return
		posted = [f for f in self.run.findings if f.kept]
		urls: dict[int, str] = {}
		if not self.run.settings.get("update_summary_only"):
			urls = self._inline([f for f in posted if not f.outside_diff], names)
		body = render.summary(self.run, posted, review_number, urls)
		self.run.review.db_set("body", body)
		if self.run.settings.get("update_description"):
			text = render.description(self.run.description, body.replace(render.SUMMARY_MARKER, ""))
			self.gh.update_description(self.repo, self.number, text)
		elif self.run.pr.summary_comment_id:
			try:
				self.gh.update_comment(self.repo, self.number, self.run.pr.summary_comment_id, body)
			except ProviderError:
				self._new_summary(body)
		else:
			self._new_summary(body)

	def _new_summary(self, body: str) -> None:
		comment = self.gh.post_comment(self.repo, self.number, body)
		self.run.pr.db_set("summary_comment_id", comment.id)

	def _inline(self, findings: list[Finding], names: dict[int, str]) -> dict[int, str]:
		if not findings:
			return {}
		fix = bool(self.run.settings.get("fix_with_ai", True))
		comments = [_comment(f, names[id(f)], fix) for f in findings]
		urls: dict[int, str] = {}
		try:
			posted = self.gh.create_review(self.repo, self.number, self.run.head_sha, comments)
		except ProviderError:
			posted: list[PostedComment] = []
			for comment in comments:
				try:
					posted += self.gh.create_review(self.repo, self.number, self.run.head_sha, [comment])
				except ProviderError:
					posted.append(PostedComment(""))
		for finding, result in zip(findings, posted, strict=False):
			name = names[id(finding)]
			if result and result.id:
				db.set_values(
					"Review Finding",
					name,
					{
						"posted": 1,
						"provider_comment_id": result.id,
						"provider_thread_id": result.thread_id,
						"comment_url": result.url,
					},
				)
				urls[id(finding)] = result.url
			else:
				finding.outside_diff = True
				frappe.db.set_value("Review Finding", name, "outside_diff", 1)
		return urls

	# ------------------------------------------------------------ status

	def start(self) -> None:
		if not self.adapter or self.run.pr is None:
			return
		self._react("eyes")
		if self.run.settings.get("status_check", True):
			self._status("pending", "Guard is reviewing")

	def finish(self, failed: bool = False) -> None:
		if not self.adapter or self.run.pr is None:
			return
		self._unreact("eyes")
		self._react("confused" if failed else "+1")
		if failed:
			self._status("error", "Guard could not finish the review")
			return
		posted = [f for f in self.run.findings if f.kept]
		if self.run.settings.get("status_check", True):
			score = self.run.verdict.confidence_score
			open_count = self._open_findings()
			text = f"{score}/5 · {open_count} open finding(s)"
			self._status("success" if score == 5 and not open_count else "failure", text)
		elif not posted and self.run.settings.get("status_comments"):
			self.adapter.post_comment(
				self.repo, self.number, f"{len(self.run.files)} files reviewed, no comments."
			)

	def _open_findings(self) -> int:
		"""Open findings on the pull request, from this review and earlier ones."""
		if not self.run.review.pull_request:
			return sum(1 for f in self.run.findings if f.kept)
		return open_finding_count(str(self.run.review.pull_request))

	def cancel(self) -> None:
		if self._shows_check():
			self._status("cancelled", "Guard stopped this review before it finished")

	def skip(self, reason: str) -> None:
		if self._shows_check():
			report_skip(self.gh, self.repo, self.run.head_sha, reason, self._url())

	def _shows_check(self) -> bool:
		return bool(self.adapter and self.run.pr is not None and self.run.settings.get("status_check", True))

	def _status(self, state: str, text: str) -> None:
		set_status(self.gh, self.repo, self.run.head_sha, state, text, self._url())

	def _url(self) -> str:
		return links.code_review(str(self.run.review.name))

	def _react(self, content: str) -> None:
		try:
			reaction = self.gh.add_reaction(self.repo, self.number, "pr", "", content)
		except ProviderError:
			return
		if reaction:
			cache.set(self._reaction_key(content), reaction, 86400)

	def _unreact(self, content: str) -> None:
		reaction = cache.get(self._reaction_key(content))
		if not reaction:
			return
		try:
			self.gh.remove_reaction(self.repo, self.number, "pr", "", str(reaction))
		except ProviderError:
			pass
		cache.delete(self._reaction_key(content))

	def _reaction_key(self, content: str) -> str:
		return f"guard:reaction:{self.repo}:{self.number}:{content}"


def set_status(adapter: ProviderAdapter, repo: str, sha: str, state: str, text: str, url: str = "") -> None:
	"""Set the Guard check. A provider error is only logged."""
	try:
		adapter.set_status(repo, sha, state, text, url)
	except ProviderError as error:
		frappe.log_error(str(error), "Guard status")


def report_skip(adapter: ProviderAdapter, repo: str, sha: str, reason: str, url: str = "") -> None:
	"""Show on the commit that Guard will not review it, so nobody waits for a review."""
	set_status(adapter, repo, sha, "skipped", f"Guard skipped this review: {reason}", url)


def open_finding_count(pull_request: str) -> int:
	"""Open findings on the pull request from every review, inline or only in the summary."""
	return int(
		db.scalar(
			"SELECT COUNT(*) FROM `tabReview Finding` WHERE pull_request = %s AND status = 'Open'",
			(pull_request,),
		)
		or 0
	)


def _comment(finding: Finding, name: str, fix: bool) -> InlineComment:
	start = finding.start_line if finding.start_line < finding.end_line else None
	return InlineComment(
		finding.path, finding.end_line, render.inline(finding, name, fix), start, finding.side
	)
