"""Decide if an event starts a review, then create it."""

from typing import TYPE_CHECKING, cast

import frappe

from guard.config import gate
from guard.config.globs import match_filter
from guard.config.resolver import ConfigResolver
from guard.git.diff import parse_diff
from guard.git.repository import store_for
from guard.guard.doctype.review_settings.review_settings import get_effective
from guard.providers import adapter_for_repository
from guard.review import permissions
from guard.review.jobs import create_review
from guard.review.publish import report_skip
from guard.utils import db

if TYPE_CHECKING:
	from guard.guard.doctype.pull_request.pull_request import PullRequest


def consider(
	pull_request: str,
	event: str,
	manual: bool = False,
	requested_by: str = "",
	requested_tier: str = "",
	instructions: str = "",
	force_draft: bool = False,
) -> str | None:
	"""Run the review gate. Return the new Code Review name, or None when it is skipped."""
	pr = cast("PullRequest", frappe.get_doc("Pull Request", pull_request))
	repository = str(pr.repository)
	repo = db.values("Code Repository", repository, ("reviews_enabled", "full_name", "team")) or {}
	if not repo.get("reviews_enabled"):
		return None
	login = requested_by or str(pr.author or "")
	if not permissions.allowed("tag" if manual else "author", login, repository):
		return None
	store = store_for(repository)
	adapter = adapter_for_repository(repository)
	number = int(pr.number or 0)
	store.fetch_branches(str(pr.base_branch or ""))
	head = store.fetch_pull(adapter.pull_ref(number), number) if adapter else str(pr.head_sha)
	merge_base = store.merge_base(store.rev_parse(f"refs/heads/{pr.get('base_branch')}"), head)
	files = parse_diff(store.diff(merge_base, head))
	paths = [p for f in files for p in {f.path, f.old_path} if p]
	config = ConfigResolver.from_store(repository, store, head).resolve_pr([f.path for f in files])
	facts = gate.PullRequestFacts(
		repository=str(repo.get("full_name")),
		title=str(pr.title or ""),
		description=str(pr.description or ""),
		author=str(pr.author or ""),
		base_branch=str(pr.base_branch or ""),
		head_branch=str(pr.head_branch or ""),
		is_draft=bool(pr.is_draft) and not force_draft,
		labels=list(frappe.parse_json(pr.labels or "[]") or []),
		paths=paths,
		additions=sum(f.additions for f in files),
		deletions=sum(f.deletions for f in files),
	)
	rules = gate.dashboard_rules(get_effective(repo.get("team")))
	result = gate.check(facts, config, event, manual, rules)
	if not result.allowed:
		frappe.logger("guard").info(f"review skipped for {pull_request}: {result.reason}")
		if adapter and config.settings.get("status_check", True):
			report_skip(adapter, str(repo.get("full_name")), head, result.reason)
		return None
	trigger = event if not manual else ("retrigger" if event == "retrigger" else "mention")
	user = permissions.user_for_login(requested_by) if requested_by else None
	return create_review(pull_request, trigger, requested_by, requested_tier, instructions, user)


def push_kind(repository: str, before: str | None, after: str | None) -> str:
	"""`push` when the old head is an ancestor of the new head, else `rebase`."""
	if not before or not after or set(before) == {"0"}:
		return "push"
	store = store_for(repository)
	try:
		store.fetch_sha(after)
		store.fetch_sha(before)
	except Exception:
		return "rebase"
	return "push" if store.is_ancestor(before, after) else "rebase"


def trigger_label(pull_request: str, label: str) -> bool:
	"""A label in the `labels` filter counts as an `open` event."""
	repository = str(db.value("Pull Request", pull_request, "repository"))
	store = store_for(repository)
	head = str(db.value("Pull Request", pull_request, "head_sha") or "")
	if not head or not store.has_commit(head):
		return False
	config = ConfigResolver.from_store(repository, store, head).resolve("")
	return any(match_filter(label, [pattern]) for pattern in config.settings.get("labels") or [])
