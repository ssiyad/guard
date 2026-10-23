"""The provider-neutral interface for code hosts."""

from dataclasses import dataclass, field
from typing import Any

import httpx

from guard.utils.redact import redact


class ProviderError(Exception):
	def __init__(self, message: str, status: int = 0):
		super().__init__(message)
		self.status = status


class UnsupportedAction(ProviderError):
	pass


@dataclass
class RepositoryInfo:
	full_name: str
	provider_repo_id: str
	default_branch: str
	clone_url: str
	web_url: str
	is_private: bool = False


@dataclass
class PullRequestInfo:
	number: int
	title: str
	description: str
	author: str
	state: str  # open, closed, merged
	is_draft: bool
	base_branch: str
	head_branch: str
	base_sha: str
	head_sha: str
	web_url: str
	head_clone_url: str = ""
	labels: list[str] = field(default_factory=list)
	opened_at: str | None = None
	merged_at: str | None = None
	closed_at: str | None = None
	additions: int = 0
	deletions: int = 0
	changed_files: int = 0
	node_id: str = ""


@dataclass
class InlineComment:
	path: str
	line: int
	body: str
	start_line: int | None = None
	side: str = "RIGHT"


@dataclass
class PostedComment:
	id: str
	url: str = ""
	thread_id: str = ""


@dataclass
class ProviderComment:
	id: str
	kind: str  # issue, review, inline
	author: str
	body: str
	created_at: str = ""
	path: str | None = None
	line: int | None = None
	in_reply_to: str | None = None
	thread_id: str | None = None
	is_bot: bool = False
	number: int | None = None  # the pull request, for providers that need it


@dataclass
class Reaction:
	content: str  # +1, -1, eyes, confused, ...
	user: str
	id: str = ""


class ProviderAdapter:
	"""One subclass for each provider. Methods take `repo` as `owner/name`."""

	provider_type = ""
	default_api_url = ""

	def __init__(self, doc: Any, team: str | None = None, token: str | None = None):
		self.doc = doc
		self.team = team
		self.api_url = (doc.get("api_url") or self.default_api_url).rstrip("/")
		self.web_url = (doc.get("web_url") or "").rstrip("/")
		self.verify = bool(doc.get("verify_ssl", 1))
		self._token = token

	def with_token(self, token: str) -> "ProviderAdapter":
		"""The same adapter acting as a person, for chat actions."""
		return type(self)(self.doc, self.team, token)

	# ------------------------------------------------------------ auth

	def token(self) -> str:
		return self._token or self.doc.get_password("token", raise_exception=False) or ""

	def auth_headers(self) -> dict[str, str]:
		raise NotImplementedError

	def git_auth_header(self) -> str | None:
		raise NotImplementedError

	# ------------------------------------------------------------ http

	def request(self, method: str, path: str, auth: bool = True, **kwargs: Any) -> Any:
		url = path if path.startswith("http") else f"{self.api_url}{path}"
		headers = {**(self.auth_headers() if auth else {}), **kwargs.pop("headers", {})}
		try:
			response = httpx.request(method, url, headers=headers, timeout=60, verify=self.verify, **kwargs)
		except httpx.HTTPError as error:
			raise ProviderError(redact(f"{method} {path} failed: {error}")) from error
		if response.status_code >= 400:
			raise ProviderError(
				redact(f"{method} {path}: {response.status_code} {response.text[:300]}"), response.status_code
			)
		if not response.content:
			return None
		try:
			return response.json()
		except ValueError:
			return response.text

	def paginate(self, path: str, params: dict | None = None, limit: int = 1000) -> list[Any]:
		items: list[Any] = []
		page = 1
		while len(items) < limit:
			batch = self.request("GET", path, params={**(params or {}), "per_page": 100, "page": page})
			if not batch:
				break
			items.extend(batch)
			if len(batch) < 100:
				break
			page += 1
		return items[:limit]

	# ------------------------------------------------------------ reads

	def get_repository(self, repo: str) -> RepositoryInfo:
		raise NotImplementedError

	def list_repositories(self, namespace: str) -> list[RepositoryInfo]:
		raise NotImplementedError

	def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
		raise NotImplementedError

	def list_open_pull_requests(self, repo: str) -> list[PullRequestInfo]:
		raise NotImplementedError

	def list_comments(self, repo: str, number: int) -> list[ProviderComment]:
		raise NotImplementedError

	def list_reactions(self, repo: str, comment: ProviderComment) -> list[Reaction]:
		return []

	def is_member(self, namespace: str, login: str) -> bool:
		raise NotImplementedError

	def pull_ref(self, number: int) -> str:
		"""The ref of a pull request head. Empty when the provider has none: fetch the head branch."""
		return ""

	def commit_url(self, repo: str, sha: str) -> str:
		raise NotImplementedError

	def file_url(
		self, repo: str, sha: str, path: str, start: int | None = None, end: int | None = None
	) -> str:
		raise NotImplementedError

	# ------------------------------------------------------------ writes

	def post_comment(self, repo: str, number: int, body: str) -> PostedComment:
		raise NotImplementedError

	def update_comment(self, repo: str, number: int, comment_id: str, body: str) -> None:
		raise NotImplementedError

	def reply_to_comment(self, repo: str, number: int, comment: ProviderComment, body: str) -> PostedComment:
		raise NotImplementedError

	def create_review(
		self,
		repo: str,
		number: int,
		sha: str,
		comments: list[InlineComment],
		body: str = "",
		event: str = "COMMENT",
	) -> list[PostedComment]:
		raise NotImplementedError

	def update_description(self, repo: str, number: int, body: str) -> None:
		raise NotImplementedError

	def set_status(self, repo: str, sha: str, state: str, description: str, url: str = "") -> None:
		"""`state` is pending, success, failure, error, skipped or cancelled."""
		raise NotImplementedError

	def add_reaction(self, repo: str, number: int, target: str, target_id: str, content: str) -> str | None:
		"""`target` is `pr` or `comment`. Return the reaction id when the provider gives one."""
		return None

	def remove_reaction(self, repo: str, number: int, target: str, target_id: str, reaction_id: str) -> None:
		return None

	def resolve_thread(self, repo: str, number: int, thread_id: str) -> None:
		raise UnsupportedAction("resolve_thread")

	def unresolve_thread(self, repo: str, number: int, thread_id: str) -> None:
		raise UnsupportedAction("unresolve_thread")

	def approve(self, repo: str, number: int, sha: str, body: str = "") -> str | None:
		raise UnsupportedAction("approve")

	def paginate_as_app(self, path: str) -> list[dict[str, Any]]:
		raise UnsupportedAction("paginate_as_app")

	def dismiss_approval(self, repo: str, number: int, approval_id: str) -> None:
		raise UnsupportedAction("dismiss_approval")

	def request_changes(self, repo: str, number: int, body: str) -> None:
		raise UnsupportedAction("request_changes")

	def request_reviewers(self, repo: str, number: int, reviewers: list[str]) -> None:
		raise UnsupportedAction("request_reviewers")

	def merge(self, repo: str, number: int, method: str = "merge") -> None:
		raise UnsupportedAction("merge")

	def close(self, repo: str, number: int) -> None:
		raise UnsupportedAction("close")

	def reopen(self, repo: str, number: int) -> None:
		raise UnsupportedAction("reopen")

	def update_branch(self, repo: str, number: int) -> None:
		raise UnsupportedAction("update_branch")

	def set_draft(self, repo: str, number: int, draft: bool) -> None:
		raise UnsupportedAction("set_draft")
