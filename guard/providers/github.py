"""GitHub and GitHub Enterprise, with a GitHub App or a token."""

import base64
import time
from typing import Any

import jwt

from guard.providers.base import (
	InlineComment,
	PostedComment,
	ProviderAdapter,
	ProviderComment,
	ProviderError,
	PullRequestInfo,
	Reaction,
	RepositoryInfo,
)
from guard.utils import cache, db

TOKEN_TTL = 50 * 60
REACTION_PATHS = {"pr": "issues/{number}", "issue": "issues/comments/{id}", "inline": "pulls/comments/{id}"}


class GitHubAdapter(ProviderAdapter):
	provider_type = "GitHub"
	default_api_url = "https://api.github.com"

	@property
	def web(self) -> str:
		if self.web_url:
			return self.web_url
		return (
			"https://github.com" if "api.github.com" in self.api_url else self.api_url.replace("/api/v3", "")
		)

	# ------------------------------------------------------------ auth

	def token(self) -> str:
		if self._token:
			return self._token
		if self.doc.get("auth_mode") == "App":
			return self._installation_token()
		return super().token()

	def auth_headers(self) -> dict[str, str]:
		return {
			"Authorization": f"Bearer {self.token()}",
			"Accept": "application/vnd.github+json",
			"X-GitHub-Api-Version": "2022-11-28",
		}

	def git_auth_header(self) -> str | None:
		token = self.token()
		if not token:
			return None
		basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
		return f"Authorization: Basic {basic}"

	def _installation_token(self) -> str:
		installation = db.value("Guard Team", self.team, "installation_id") if self.team else None
		if not installation:
			raise ProviderError("This team has no GitHub App installation")
		key = f"guard:github-token:{self.doc.name}:{installation}"
		cached = cache.get(key)
		if cached:
			return str(cached)
		data = self.request_as_app("POST", f"/app/installations/{installation}/access_tokens")
		cache.set(key, data["token"], TOKEN_TTL)
		return data["token"]

	def app_jwt(self) -> str:
		now = int(time.time())
		key = self.doc.get_password("private_key", raise_exception=False) or ""
		payload = {"iat": now - 60, "exp": now + 540, "iss": str(self.doc.get("app_id"))}
		return jwt.encode(payload, key, algorithm="RS256")

	def paginate_as_app(self, path: str) -> list[dict[str, Any]]:
		items, page = [], 1
		while True:
			batch = self.request_as_app("GET", path, params={"per_page": 100, "page": page}) or []
			items += batch
			if len(batch) < 100:
				return items
			page += 1

	def request_as_app(self, method: str, path: str, **kwargs: Any) -> Any:
		headers = {"Authorization": f"Bearer {self.app_jwt()}", "Accept": "application/vnd.github+json"}
		return self.request(method, path, auth=False, headers=headers, **kwargs)

	def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
		url = (
			f"{self.api_url}/graphql"
			if "api.github.com" in self.api_url
			else self.api_url.replace("/v3", "/graphql")
		)
		data = self.request("POST", url, json={"query": query, "variables": variables}) or {}
		if data.get("errors"):
			raise ProviderError(str(data["errors"])[:500])
		return data.get("data") or {}

	# ------------------------------------------------------------ reads

	def get_repository(self, repo: str) -> RepositoryInfo:
		return _repository(self.request("GET", f"/repos/{repo}"))

	def list_repositories(self, namespace: str) -> list[RepositoryInfo]:
		if self.doc.get("auth_mode") == "App":
			items, page = [], 1
			while True:
				data = self.request(
					"GET", "/installation/repositories", params={"per_page": 100, "page": page}
				)
				batch = data.get("repositories") or []
				items += batch
				if len(batch) < 100:
					break
				page += 1
			return [_repository(r) for r in items]
		try:
			return [_repository(r) for r in self.paginate(f"/orgs/{namespace}/repos")]
		except ProviderError:
			return [_repository(r) for r in self.paginate(f"/users/{namespace}/repos")]

	def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
		return pull_request_info(self.request("GET", f"/repos/{repo}/pulls/{number}"))

	def list_open_pull_requests(self, repo: str) -> list[PullRequestInfo]:
		return [
			pull_request_info(p) for p in self.paginate(f"/repos/{repo}/pulls", {"state": "open"}, limit=200)
		]

	def list_comments(self, repo: str, number: int) -> list[ProviderComment]:
		comments = [
			ProviderComment(
				str(c["id"]),
				"issue",
				_login(c),
				c.get("body") or "",
				c.get("created_at") or "",
				is_bot=_is_bot(c),
			)
			for c in self.paginate(f"/repos/{repo}/issues/{number}/comments")
		]
		for c in self.paginate(f"/repos/{repo}/pulls/{number}/comments"):
			reply_to = c.get("in_reply_to_id")
			comments.append(
				ProviderComment(
					str(c["id"]),
					"inline",
					_login(c),
					c.get("body") or "",
					c.get("created_at") or "",
					path=c.get("path"),
					line=c.get("line") or c.get("original_line"),
					in_reply_to=str(reply_to) if reply_to else None,
					thread_id=str(reply_to or c["id"]),
					is_bot=_is_bot(c),
				)
			)
		for r in self.paginate(f"/repos/{repo}/pulls/{number}/reviews"):
			if r.get("body"):
				comments.append(
					ProviderComment(
						str(r["id"]),
						"review",
						_login(r),
						r["body"],
						r.get("submitted_at") or "",
						is_bot=_is_bot(r),
					)
				)
		return comments

	def list_reactions(self, repo: str, comment: ProviderComment) -> list[Reaction]:
		kind = "inline" if comment.kind == "inline" else "issue"
		path = REACTION_PATHS[kind].format(id=comment.id, number=0)
		data = self.paginate(f"/repos/{repo}/{path}/reactions")
		return [Reaction(r["content"], _login(r), str(r["id"])) for r in data]

	def is_member(self, namespace: str, login: str) -> bool:
		try:
			self.request("GET", f"/orgs/{namespace}/members/{login}")
		except ProviderError:
			return False
		return True

	def pull_ref(self, number: int) -> str:
		return f"refs/pull/{number}/head"

	def commit_url(self, repo: str, sha: str) -> str:
		return f"{self.web}/{repo}/commit/{sha}"

	def file_url(
		self, repo: str, sha: str, path: str, start: int | None = None, end: int | None = None
	) -> str:
		anchor = f"#L{start}" + (f"-L{end}" if end and end != start else "") if start else ""
		return f"{self.web}/{repo}/blob/{sha}/{path}{anchor}"

	# ------------------------------------------------------------ writes

	def post_comment(self, repo: str, number: int, body: str) -> PostedComment:
		data = self.request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})
		return PostedComment(str(data["id"]), data.get("html_url") or "")

	def update_comment(self, repo: str, number: int, comment_id: str, body: str) -> None:
		self.request("PATCH", f"/repos/{repo}/issues/comments/{comment_id}", json={"body": body})

	def reply_to_comment(self, repo: str, number: int, comment: ProviderComment, body: str) -> PostedComment:
		if comment.kind != "inline":
			return self.post_comment(repo, number, body)
		root = comment.in_reply_to or comment.id
		data = self.request(
			"POST", f"/repos/{repo}/pulls/{number}/comments/{root}/replies", json={"body": body}
		)
		return PostedComment(str(data["id"]), data.get("html_url") or "", str(root))

	def create_review(
		self,
		repo: str,
		number: int,
		sha: str,
		comments: list[InlineComment],
		body: str = "",
		event: str = "COMMENT",
	) -> list[PostedComment]:
		payload: dict[str, Any] = {
			"commit_id": sha,
			"event": event,
			"comments": [_inline(c) for c in comments],
		}
		if body:
			payload["body"] = body
		review = self.request("POST", f"/repos/{repo}/pulls/{number}/reviews", json=payload)
		posted = self.paginate(f"/repos/{repo}/pulls/{number}/reviews/{review['id']}/comments")
		by_key = {(c.get("path"), c.get("body")): c for c in posted}
		result = []
		for comment in comments:
			match = by_key.get((comment.path, comment.body))
			result.append(
				PostedComment(str(match["id"]), match.get("html_url") or "", str(match["id"]))
				if match
				else PostedComment("")
			)
		return result

	def update_description(self, repo: str, number: int, body: str) -> None:
		self.request("PATCH", f"/repos/{repo}/pulls/{number}", json={"body": body})

	def set_status(self, repo: str, sha: str, state: str, description: str, url: str = "") -> None:
		if self.doc.get("auth_mode") == "App":
			self._check_run(repo, sha, state, description, url)
			return
		# A commit status has no skipped state.
		payload = {
			"state": "success" if state == "skipped" else state,
			"description": description[:140],
			"context": "Guard",
		}
		if url:
			payload["target_url"] = url
		self.request("POST", f"/repos/{repo}/statuses/{sha}", json=payload)

	def _check_run(self, repo: str, sha: str, state: str, description: str, url: str) -> None:
		key = f"guard:check-run:{repo}:{sha}"
		run_id = cache.get(key)
		payload: dict[str, Any] = {
			"name": "Guard",
			"head_sha": sha,
			"output": {"title": description[:200], "summary": description},
		}
		if url:
			payload["details_url"] = url
		if state == "pending":
			payload["status"] = "in_progress"
		else:
			payload["status"] = "completed"
			payload["conclusion"] = {
				"success": "success",
				"failure": "failure",
				"error": "neutral",
				"skipped": "skipped",
			}.get(state, "neutral")
		if run_id:
			self.request("PATCH", f"/repos/{repo}/check-runs/{run_id}", json=payload)
			return
		data = self.request("POST", f"/repos/{repo}/check-runs", json=payload)
		cache.set(key, data["id"], 86400)

	def add_reaction(self, repo: str, number: int, target: str, target_id: str, content: str) -> str | None:
		path = REACTION_PATHS[target].format(id=target_id, number=number)
		data = self.request("POST", f"/repos/{repo}/{path}/reactions", json={"content": content})
		return str(data["id"]) if data else None

	def remove_reaction(self, repo: str, number: int, target: str, target_id: str, reaction_id: str) -> None:
		path = REACTION_PATHS[target].format(id=target_id, number=number)
		self.request("DELETE", f"/repos/{repo}/{path}/reactions/{reaction_id}")

	def resolve_thread(self, repo: str, number: int, thread_id: str) -> None:
		node = self._thread_node(repo, number, thread_id)
		if node:
			self.graphql(
				"mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { id } } }",
				{"id": node},
			)

	def unresolve_thread(self, repo: str, number: int, thread_id: str) -> None:
		node = self._thread_node(repo, number, thread_id)
		if node:
			self.graphql(
				"mutation($id: ID!) { unresolveReviewThread(input: {threadId: $id}) { thread { id } } }",
				{"id": node},
			)

	def _thread_node(self, repo: str, number: int, comment_id: str) -> str | None:
		owner, name = repo.split("/", 1)
		query = """query($owner: String!, $name: String!, $number: Int!, $after: String) {
			repository(owner: $owner, name: $name) { pullRequest(number: $number) {
				reviewThreads(first: 100, after: $after) { pageInfo { hasNextPage endCursor }
					nodes { id comments(first: 1) { nodes { databaseId } } } } } } }"""
		after = None
		for _ in range(10):
			data = self.graphql(query, {"owner": owner, "name": name, "number": number, "after": after})
			threads = data["repository"]["pullRequest"]["reviewThreads"]
			for node in threads["nodes"]:
				first = node["comments"]["nodes"]
				if first and str(first[0]["databaseId"]) == str(comment_id):
					return node["id"]
			if not threads["pageInfo"]["hasNextPage"]:
				return None
			after = threads["pageInfo"]["endCursor"]
		return None

	def approve(self, repo: str, number: int, sha: str, body: str = "") -> str | None:
		data = self.request(
			"POST",
			f"/repos/{repo}/pulls/{number}/reviews",
			json={"commit_id": sha, "event": "APPROVE", "body": body},
		)
		return str(data["id"])

	def dismiss_approval(self, repo: str, number: int, approval_id: str) -> None:
		self.request(
			"PUT",
			f"/repos/{repo}/pulls/{number}/reviews/{approval_id}/dismissals",
			json={"message": "New commits since the approval.", "event": "DISMISS"},
		)

	def request_changes(self, repo: str, number: int, body: str) -> None:
		self.request(
			"POST", f"/repos/{repo}/pulls/{number}/reviews", json={"event": "REQUEST_CHANGES", "body": body}
		)

	def request_reviewers(self, repo: str, number: int, reviewers: list[str]) -> None:
		self.request(
			"POST", f"/repos/{repo}/pulls/{number}/requested_reviewers", json={"reviewers": reviewers}
		)

	def merge(self, repo: str, number: int, method: str = "merge") -> None:
		self.request("PUT", f"/repos/{repo}/pulls/{number}/merge", json={"merge_method": method})

	def close(self, repo: str, number: int) -> None:
		self.request("PATCH", f"/repos/{repo}/pulls/{number}", json={"state": "closed"})

	def reopen(self, repo: str, number: int) -> None:
		self.request("PATCH", f"/repos/{repo}/pulls/{number}", json={"state": "open"})

	def update_branch(self, repo: str, number: int) -> None:
		self.request("PUT", f"/repos/{repo}/pulls/{number}/update-branch", json={})

	def set_draft(self, repo: str, number: int, draft: bool) -> None:
		node = self.get_pull_request(repo, number).node_id
		mutation = "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview"
		self.graphql(
			f"mutation($id: ID!) {{ {mutation}(input: {{pullRequestId: $id}}) {{ clientMutationId }} }}",
			{"id": node},
		)


def pull_request_info(data: dict[str, Any]) -> PullRequestInfo:
	state = "merged" if data.get("merged_at") else data.get("state", "open")
	head, base = data.get("head") or {}, data.get("base") or {}
	return PullRequestInfo(
		number=int(data["number"]),
		title=data.get("title") or "",
		description=data.get("body") or "",
		author=_login(data),
		state=state,
		is_draft=bool(data.get("draft")),
		base_branch=base.get("ref") or "",
		head_branch=head.get("ref") or "",
		base_sha=base.get("sha") or "",
		head_sha=head.get("sha") or "",
		web_url=data.get("html_url") or "",
		head_clone_url=(head.get("repo") or {}).get("clone_url") or "",
		labels=[label["name"] for label in data.get("labels") or []],
		opened_at=data.get("created_at"),
		merged_at=data.get("merged_at"),
		closed_at=data.get("closed_at"),
		additions=data.get("additions") or 0,
		deletions=data.get("deletions") or 0,
		changed_files=data.get("changed_files") or 0,
		node_id=data.get("node_id") or "",
	)


def _repository(data: dict[str, Any]) -> RepositoryInfo:
	return RepositoryInfo(
		full_name=data["full_name"],
		provider_repo_id=str(data["id"]),
		default_branch=data.get("default_branch") or "main",
		clone_url=data.get("clone_url") or "",
		web_url=data.get("html_url") or "",
		is_private=bool(data.get("private")),
	)


def _inline(comment: InlineComment) -> dict[str, Any]:
	payload: dict[str, Any] = {
		"path": comment.path,
		"line": comment.line,
		"side": comment.side,
		"body": comment.body,
	}
	if comment.start_line and comment.start_line < comment.line:
		payload["start_line"] = comment.start_line
		payload["start_side"] = comment.side
	return payload


def _login(data: dict[str, Any]) -> str:
	return (data.get("user") or {}).get("login") or ""


def _is_bot(data: dict[str, Any]) -> bool:
	return (data.get("user") or {}).get("type") == "Bot"
