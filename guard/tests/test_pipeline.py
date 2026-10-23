from unittest.mock import PropertyMock, patch

import frappe

from guard.llm.client import LLMClient, use_responder
from guard.providers.base import ProviderComment
from guard.review import trigger
from guard.tests.cases import AUTO_APPROVE, PullRequestCase
from guard.tests.fakes import scripted
from guard.utils import db


class TestPipeline(PullRequestCase):
	def test_review_posts_findings_summary_and_status(self):
		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P1",
			"comment_type": "logic",
			"title": "Caller change_email is not updated for the new notify argument",
			"body": "`app/service.py:6` calls `update_user_email(user, email)`. It raises TypeError now.",
			"confidence": 0.9,
		}
		name = self.review(
			[
				[("read_file", {"path": "app/service.py"})],
				[
					("report_finding", finding),
					(
						"submit_review",
						{"summary": "Adds a notify flag.", "confidence_score": 5, "risk_level": "medium"},
					),
				],
			]
		)
		review = self.row(name)
		self.assertEqual(review["status"], "Completed", review["error"])
		self.assertEqual(review["posted_count"], 1)
		self.assertEqual(review["confidence_score"], 3)  # capped by the P1
		self.assertEqual(review["credits"], 3)  # a first review is at least plus
		posted = self.adapter.named("create_review")[0]
		self.assertEqual(posted[0].path, "app/models.py")
		self.assertIn("**P1**", posted[0].body)
		summary = self.adapter.named("post_comment")[0]
		self.assertIn("<!-- guard:summary -->", summary)
		self.assertIn("Confidence **3/5**", summary)
		self.assertIn("Affects 1 caller in 1 file. 1 caller may break.", summary)
		self.assertIn("<summary><b>Impact</b></summary>", summary)
		self.assertIn("-.->|may break|", summary)
		self.assertIn("Reviews (1)", summary)
		self.assertEqual(self.adapter.named("status"), ["pending", "failure"])
		self.assertIn("+1", self.adapter.named("react"))

	def test_review_waits_while_the_index_is_busy(self):
		from guard.indexing.indexer import index_lock
		from guard.indexing.jobs import index_for
		from guard.review import jobs

		real, retries = jobs.enqueue_review, []

		def enqueue(review, delay=0, attempt=0, tier=""):
			if attempt:
				retries.append((delay, attempt))
				return
			real(review, delay=delay, attempt=attempt, tier=tier)

		with (
			index_lock(index_for(self.repository)),
			patch("guard.review.jobs.enqueue_review", side_effect=enqueue),
		):
			name = self.review([])
		row = db.values("Code Review", str(name), ("status", "stage")) or {}
		self.assertEqual((row["status"], row["stage"]), ("Queued", "waiting 60s for the code graph index"))
		self.assertEqual(retries, [(60, 1)])

	def test_dead_review_job_fails_the_review(self):
		from frappe.utils import now_datetime

		from guard.review.jobs import fail_dead_reviews

		# What a worker restart leaves: Running, and no job in the queue.
		dead = frappe.get_doc(
			{
				"doctype": "Code Review",
				"pull_request": self.pr,
				"repository": self.repository,
				"source": "pull_request",
				"status": "Running",
				"trigger": "open",
				"head_sha": self.head,
				"started_at": now_datetime(),
			}
		).insert(ignore_permissions=True)
		with patch("guard.review.jobs.is_job_enqueued", return_value=True):
			fail_dead_reviews()
		self.assertEqual(db.value("Code Review", str(dead.name), "status"), "Running")
		fail_dead_reviews()
		self.assertEqual(db.value("Code Review", str(dead.name), "status"), "Failed")
		self.assertEqual(self.adapter.named("status"), ["error"])
		self.assertIn("confused", self.adapter.named("react"))

	def test_cancelled_review_closes_the_check(self):
		def cancel(repository):
			frappe.db.set_value("Code Review", {"pull_request": self.pr}, "cancel_requested", 1)

		with patch("guard.review.pipeline.ensure_fresh", side_effect=cancel):
			name = self.review([])
		self.assertEqual(self.row(name)["status"], "Cancelled")
		self.assertEqual(self.adapter.named("status"), ["pending", "cancelled"])

	def test_credit_cap_closes_the_check(self):
		frappe.db.set_single_value("Guard Settings", "monthly_credit_cap", 1)
		name = self.review([])
		self.assertEqual(self.row(name)["status"], "Skipped")
		self.assertEqual(self.adapter.named("status"), ["pending", "skipped"])

	def test_rereview_edits_summary_and_dedupes(self):
		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P1",
			"comment_type": "logic",
			"title": "Callers are not updated",
			"body": "x",
			"confidence": 0.9,
		}
		self.review(
			[[("report_finding", finding), ("submit_review", {"summary": "s", "confidence_score": 4})]]
		)
		with use_responder(
			scripted(
				[[("report_finding", finding), ("submit_review", {"summary": "s", "confidence_score": 4})]]
			)
		):
			second = trigger.consider(self.pr, "open", manual=True, requested_by="ada")
		review = self.row(second)
		self.assertEqual(review["status"], "Completed", review["error"])
		self.assertEqual(review["posted_count"], 0)
		self.assertEqual(review["review_number"], 2)
		self.assertEqual(len(self.adapter.named("update_comment")), 1)
		# Raised again, so it still applies: the thread stays open.
		self.assertFalse(self.adapter.named("resolve"))

	def test_rereview_resolves_fixed_findings(self):
		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P1",
			"comment_type": "logic",
			"title": "Callers are not updated",
			"body": "x",
			"confidence": 0.9,
		}
		self.review(
			[[("report_finding", finding), ("submit_review", {"summary": "s", "confidence_score": 3})]]
		)
		with (
			patch("guard.review.stale._state", return_value="fixed"),
			use_responder(scripted([[("submit_review", {"summary": "s", "confidence_score": 5})]])),
		):
			trigger.consider(self.pr, "open", manual=True, requested_by="ada")
		self.assertEqual(
			frappe.get_all("Review Finding", filters={"pull_request": self.pr}, pluck="status"), ["Addressed"]
		)
		self.assertEqual(len(self.adapter.named("resolve")), 1)
		self.assertEqual(self.adapter.named("status")[-1], "success")

	def test_rereview_drops_new_p2(self):
		self.review([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		nit = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P2",
			"comment_type": "style",
			"title": "Name is vague",
			"body": "x",
			"confidence": 0.9,
		}
		with use_responder(
			scripted([[("report_finding", nit), ("submit_review", {"summary": "s", "confidence_score": 4})]])
		):
			second = trigger.consider(self.pr, "open", manual=True, requested_by="ada")
		self.assertEqual(self.row(second)["posted_count"], 0)
		# Only a dropped P2 was left, so 4 becomes 5.
		self.assertEqual(self.row(second)["confidence_score"], 5)

	def test_rereview_posts_p2_on_new_lines(self):
		self.review([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		models = (self.repo.path / "app/models.py").read_text() + "\n\nVALUE = 1\n"
		self.repo.commit({"app/models.py": models}, "add value")
		frappe.db.set_value("Pull Request", self.pr, "head_sha", self.repo.head())
		nit = {
			"path": "app/models.py",
			"start_line": len(models.splitlines()),
			"severity": "P2",
			"comment_type": "style",
			"title": "Name is vague",
			"body": "x",
			"confidence": 0.9,
		}
		with use_responder(
			scripted([[("report_finding", nit), ("submit_review", {"summary": "s", "confidence_score": 4})]])
		):
			second = trigger.consider(self.pr, "open", manual=True, requested_by="ada")
		self.assertEqual(self.row(second)["posted_count"], 1)

	def test_project_test_rule_without_a_model_finding(self):
		self.commit_on_main({"CLAUDE.md": "- Always add tests for behavior changes.\n"})
		name = self.review([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		review = self.row(name)
		self.assertEqual(review["posted_count"], 1)
		self.assertEqual(review["confidence_score"], 3)
		self.assertIn("Code changes without tests", self.adapter.named("create_review")[0][0].body)

	def test_verifier_keeps_p0_for_a_proven_break(self):
		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P0",
			"comment_type": "logic",
			"title": "change_email now raises TypeError",
			"body": "x",
			"confidence": 0.9,
		}
		lower = '[{"index": 0, "valid": true, "severity": "P1", "confidence": 0.8}]'
		# The test site has no gateway: put the clients online so the verifier runs.
		with (
			patch.object(LLMClient, "online", new_callable=PropertyMock, return_value=True),
			use_responder(
				scripted(
					[
						[
							("report_finding", finding),
							("submit_review", {"summary": "s", "confidence_score": 2}),
						]
					],
					{"verify": lower},
				)
			),
		):
			trigger.consider(self.pr, "open")
		severities = frappe.get_all("Review Finding", filters={"pull_request": self.pr}, pluck="severity")
		self.assertEqual(severities, ["P0"])

	def test_gate_skips_drafts_and_disabled_repositories(self):
		frappe.db.set_value("Pull Request", self.pr, "is_draft", 1)
		self.assertIsNone(trigger.consider(self.pr, "open"))
		frappe.db.set_value("Pull Request", self.pr, "is_draft", 0)
		frappe.db.set_value("Code Repository", self.repository, "reviews_enabled", 0)
		self.assertIsNone(trigger.consider(self.pr, "open"))

	def test_skipped_review_marks_the_commit(self):
		frappe.db.set_value("Pull Request", self.pr, "is_draft", 1)
		self.assertIsNone(trigger.consider(self.pr, "open"))
		self.assertEqual(self.adapter.named("status"), ["skipped"])
		# A repository without reviews gets no check.
		frappe.db.set_value("Code Repository", self.repository, "reviews_enabled", 0)
		self.assertIsNone(trigger.consider(self.pr, "open"))
		self.assertEqual(self.adapter.named("status"), ["skipped"])

	def test_skip_authors_from_review_settings(self):
		settings = frappe.get_doc("Review Settings", "Organization")
		settings.set("exclude_authors", "mergify[bot]\nad*")
		settings.save(ignore_permissions=True)
		try:
			self.assertIsNone(trigger.consider(self.pr, "open"))
			# Skipped authors apply to requested reviews too.
			self.assertIsNone(trigger.consider(self.pr, "open", manual=True, requested_by="bob"))
		finally:
			settings.set("exclude_authors", "")
			settings.save(ignore_permissions=True)
		with use_responder(scripted([[("submit_review", {"summary": "s", "confidence_score": 5})]])):
			self.assertTrue(trigger.consider(self.pr, "open"))

	def commit_on_main(self, files):
		"""Commit on the base branch and merge it into the pull request."""
		self.repo.git("checkout", "-q", "main")
		self.repo.commit(files, "on main")
		self.repo.git("checkout", "-q", "feature")
		self.repo.git("merge", "-q", "main", "-m", "merge main")
		frappe.db.set_value("Pull Request", self.pr, "head_sha", self.repo.head())

	def test_auto_approve_clean_low_risk(self):
		self.commit_on_main({".guard/config.toml": AUTO_APPROVE})
		name = self.review(
			[[("submit_review", {"summary": "Small change.", "confidence_score": 5, "risk_level": "low"})]]
		)
		self.assertEqual(frappe.db.get_value("Code Review", name, "status"), "Completed")
		self.assertTrue(self.adapter.named("approve"))
		self.assertEqual(self.adapter.named("status")[-1], "success")
		self.assertTrue(db.value("Pull Request", self.pr, "guard_approved"))

	def test_auto_approve_waits_for_earlier_findings(self):
		self.commit_on_main({".guard/config.toml": AUTO_APPROVE})
		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P1",
			"comment_type": "logic",
			"title": "Callers are not updated",
			"body": "x",
			"confidence": 0.9,
		}
		self.review(
			[[("report_finding", finding), ("submit_review", {"summary": "s", "confidence_score": 3})]]
		)
		# A finding shown only in the summary still blocks approval.
		frappe.db.set_value("Review Finding", {"pull_request": self.pr}, "posted", 0)
		clean = {"summary": "s", "confidence_score": 5, "risk_level": "low"}
		with use_responder(scripted([[("submit_review", clean)]])):
			second = trigger.consider(self.pr, "open", manual=True, requested_by="ada")
		self.assertEqual(self.row(second)["confidence_score"], 5)
		self.assertFalse(self.adapter.named("approve"))

	def test_mention_answers_question(self):
		from guard.conversation import threads

		comment = ProviderComment(
			"c1", "issue", "ada", "@guard why does this change the signature?", number=7
		)
		with use_responder(
			scripted([[("read_file", {"path": "app/models.py"})], "It adds `notify` to `update_user_email`."])
		):
			with patch("guard.llm.client.LLMClient.online", new=True):
				threads.handle(self.pr, comment)
		self.assertEqual(self.adapter.named("reply"), ["It adds `notify` to `update_user_email`."])

	def on_comment(self, comment_id):
		"""Reactions on one comment, in order: added contents and "removed"."""
		return [r[2] for r in self.adapter.named("reaction") if r[1] == comment_id]

	def test_mention_reactions_for_a_question(self):
		from guard.conversation import threads

		comment = ProviderComment("c2", "issue", "ada", "@guard is this safe?", number=7)
		with use_responder(scripted(["Yes."])), patch("guard.llm.client.LLMClient.online", new=True):
			threads.handle(self.pr, comment)
		self.assertEqual(self.on_comment("c2"), ["eyes", "removed", "+1"])

	def test_mention_reactions_for_a_review(self):
		from guard.conversation import threads

		comment = ProviderComment("c3", "issue", "ada", "@guard review", number=7)
		with use_responder(scripted([[("submit_review", {"summary": "s", "confidence_score": 4})]])):
			threads.handle(self.pr, comment)
		self.assertEqual(self.on_comment("c3"), ["eyes", "removed", "+1"])

	def test_mention_reactions_when_the_review_is_skipped(self):
		from guard.conversation import threads

		frappe.db.set_value("Code Repository", self.repository, "reviews_enabled", 0)
		threads.handle(self.pr, ProviderComment("c4", "issue", "ada", "@guard review", number=7))
		self.assertEqual(self.on_comment("c4"), ["eyes", "removed"])

	def test_polling_opens_and_syncs(self):
		from guard.webhooks import polling

		frappe.db.set_single_value("Guard Settings", "polling_enabled", 1)
		frappe.delete_doc("Pull Request", self.pr, force=True)
		self.adapter.list_open_pull_requests = lambda repo: [self.info]
		self.adapter.list_comments = lambda repo, number: []
		with (
			patch("guard.webhooks.polling.adapter_for_repository", return_value=self.adapter),
			use_responder(scripted([])),
		):
			polling.poll_repository(self.repository)
		pr = db.value("Pull Request", {"repository": self.repository, "number": 7}, "name")
		self.assertTrue(pr)
		self.assertEqual(frappe.db.count("Code Review", {"pull_request": pr, "trigger": "open"}), 1)
		with (
			patch("guard.webhooks.polling.adapter_for_repository", return_value=self.adapter),
			use_responder(scripted([])),
		):
			polling.poll_repository(self.repository)
		self.assertEqual(frappe.db.count("Code Review", {"pull_request": pr}), 1)
