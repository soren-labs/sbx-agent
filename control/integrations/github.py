"""Exact-subject Git effects via GitHub Git Database API; manual PAT only."""

import base64
import hashlib
from urllib.parse import quote

from control.domain.errors import DomainError, require


class GitHubEffects:
    def __init__(self, connector, objects):
        self.connector, self.objects = connector, objects

    @staticmethod
    def path(repository):
        return "/repos/" + repository.removeprefix("https://github.com/").removesuffix(".git")

    @staticmethod
    def response(result, codes=(200, 201)):
        if result.status_code not in codes:
            raise DomainError(
                "remote_head_changed" if result.status_code in {409, 422} else "delivery_unresolved"
            )
        return result.json()

    def materialize(self, credential, delivery, changeset):
        manifest = changeset["manifest"]
        subject = manifest["subject"]
        require(subject["base_sha"] is not None, "stale_subject")
        path = self.path(delivery["repository"])
        with self.connector.client(credential) as client:
            if subject.get("head_sha"):
                commit = self.response(client.get(path + "/git/commits/" + subject["head_sha"]))
                require(commit["tree"]["sha"] == subject["tree_sha"], "stale_subject")
                return commit["sha"], {
                    "head": commit["sha"],
                    "tree": commit["tree"]["sha"],
                    "mapping": "head-pinned",
                }
            base = self.response(client.get(path + "/git/commits/" + subject["base_sha"]))
            tree = []
            for entry in subject["files"]:
                item = {"path": entry["path"], "mode": entry["mode"], "type": "blob", "sha": None}
                if entry["type"] != "deleted":
                    body = self.objects.get(
                        delivery["workspace_id"], manifest["blobs"][entry["path"]]
                    )
                    blob = self.response(
                        client.post(
                            path + "/git/blobs",
                            json={"content": base64.b64encode(body).decode(), "encoding": "base64"},
                        )
                    )
                    expected = hashlib.sha1(
                        b"blob " + str(len(body)).encode() + b"\0" + body
                    ).hexdigest()
                    require(blob["sha"] == expected, "stale_subject")
                    item["sha"] = blob["sha"]
                tree.append(item)
            result = self.response(
                client.post(
                    path + "/git/trees", json={"base_tree": base["tree"]["sha"], "tree": tree}
                )
            )
            commit = self.response(
                client.post(
                    path + "/git/commits",
                    json={
                        "message": "SBX ChangeSet " + changeset["id"],
                        "tree": result["sha"],
                        "parents": [subject["base_sha"]],
                        "author": {
                            "name": "SBX",
                            "email": "sbx@users.noreply.github.com",
                            "date": "2000-01-01T00:00:00Z",
                        },
                        "committer": {
                            "name": "SBX",
                            "email": "sbx@users.noreply.github.com",
                            "date": "2000-01-01T00:00:00Z",
                        },
                    },
                )
            )
            return commit["sha"], {
                "head": commit["sha"],
                "tree": result["sha"],
                "base": subject["base_sha"],
                "subject_digest": changeset["subject_digest"],
                "mapping": "deterministic-tree-commit",
            }

    def push(self, credential, delivery, head):
        path, ref = self.path(delivery["repository"]), delivery["target_ref"]
        with self.connector.client(credential) as client:
            endpoint = path + "/git/ref/heads/" + quote(ref, safe="/")
            previous = client.get(endpoint)
            if previous.status_code == 200:
                actual = previous.json()["object"]["sha"]
                if actual == head:
                    return {"head": head, "ref": ref, "adopted": True}
                # REST ref PATCH has no atomic expected-old gate. Fail closed.
                raise DomainError("remote_head_changed")
            elif previous.status_code == 404:
                require(delivery["expected_head"] is None, "remote_head_changed")
                self.response(
                    client.post(path + "/git/refs", json={"ref": "refs/heads/" + ref, "sha": head})
                )
            else:
                raise DomainError("delivery_unresolved")
            verified = self.response(client.get(endpoint))
            require(verified["object"]["sha"] == head, "remote_head_changed")
            return {"head": head, "ref": ref, "adopted": False}

    def pull_request(self, credential, delivery, head):
        path = self.path(delivery["repository"])
        owner = delivery["repository"].removeprefix("https://github.com/").split("/")[0]
        marker = "<!-- sbx-delivery:" + delivery["id"] + " -->"
        with self.connector.client(credential) as client:
            prs = self.response(
                client.get(
                    path + "/pulls",
                    params={
                        "state": "all",
                        "head": owner + ":" + delivery["target_ref"],
                        "base": delivery["base_ref"],
                    },
                )
            )
            matching = [pr for pr in prs if marker in (pr.get("body") or "")]
            if matching:
                pr = matching[0]
            else:
                require(not prs, "delivery_unresolved")
                pr = self.response(
                    client.post(
                        path + "/pulls",
                        json={
                            "title": "SBX disposable benchmark " + delivery["changeset_id"],
                            "head": delivery["target_ref"],
                            "base": delivery["base_ref"],
                            "draft": delivery["policy"]["draft"],
                            "body": marker + "\nImmutable subject: " + delivery["subject_digest"],
                        },
                    )
                )
            require(pr["head"]["sha"] == head, "remote_head_changed")
            return {"pr_number": pr["number"], "pr_url": pr["html_url"], "head": head}

    def observe(self, credential, delivery):
        path = self.path(delivery["repository"])
        with self.connector.client(credential) as client:
            pr = self.response(client.get(path + "/pulls/" + str(delivery["pr_number"])))
            checks = self.response(
                client.get(path + "/commits/" + pr["head"]["sha"] + "/check-runs")
            )
            return {
                "head": pr["head"]["sha"],
                "base": pr["base"]["sha"],
                "draft": pr["draft"],
                "merged": pr["merged"],
                "merge_commit_sha": pr.get("merge_commit_sha"),
                "mergeable": pr.get("mergeable") is True,
                "checks": {
                    c["name"]: c["conclusion"] if c["status"] == "completed" else "pending"
                    for c in checks.get("check_runs", [])
                },
            }

    def merge(self, credential, delivery, request):
        with self.connector.client(credential) as client:
            response = client.put(
                self.path(delivery["repository"])
                + "/pulls/"
                + str(delivery["pr_number"])
                + "/merge",
                json={"sha": request["expected_head"], "merge_method": request["method"]},
            )
            value = self.response(response)
            require(value.get("merged") is True, "delivery_unresolved")
            return {"merged": True, "sha": value["sha"], "expected_head": request["expected_head"]}
