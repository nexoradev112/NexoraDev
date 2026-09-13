"""Pull-based outbound campaign dispatcher.

Run one instance per workspace API key. The control plane performs all tenant,
consent, suppression, schedule, concurrency, circuit-breaker, and billing checks
before LiveKit receives a destination number.
"""
import json
import os
import time
import urllib.error
import urllib.request

from rails import validate_control_plane_url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_CONTROL_PLANE_OPENER = urllib.request.build_opener(_NoRedirect())


def request(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    app_url = validate_control_plane_url(os.environ.get("APP_URL", ""))
    token = os.environ.get("WORKSPACE_API_KEY", "")
    if len(token) < 32:
        raise RuntimeError("WORKSPACE_API_KEY must be a revocable operator key")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
    if body is not None:
        headers["content-type"] = "application/json"
    call = urllib.request.Request(f"{app_url}{path}", data=body, method=method, headers=headers)
    try:
        with _CONTROL_PLANE_OPENER.open(call, timeout=20) as response:
            raw = response.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024 or response.headers.get_content_type() != "application/json":
                raise ValueError("invalid response envelope")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise TypeError("invalid response object")
            return result
    except urllib.error.HTTPError as error:
        if error.code in {402, 409, 429, 503}:
            return {"deferred": True, "status": error.code}
        raise RuntimeError(f"Control plane rejected dispatch with status {error.code}") from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise RuntimeError("Control plane is unavailable") from error


def dispatch_once() -> int:
    campaigns = request("/api/campaigns").get("campaigns", [])
    dispatched = 0
    for campaign in campaigns:
        if campaign.get("status") != "running" or not isinstance(campaign.get("id"), int):
            continue
        result = request("/api/campaigns/dispatch", "POST", {"campaignId": campaign["id"]})
        if result.get("dispatched"):
            dispatched += 1
    return dispatched


def main() -> None:
    interval = min(30.0, max(0.25, float(os.environ.get("DISPATCH_POLL_SECONDS", "1"))))
    while True:
        try:
            count = dispatch_once()
            time.sleep(0.25 if count else interval)
        except RuntimeError as error:
            print(str(error), flush=True)
            time.sleep(min(30.0, interval * 4))


if __name__ == "__main__":
    main()
