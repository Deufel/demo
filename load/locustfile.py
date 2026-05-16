import time
from locust import HttpUser, task, between, events


class SseViewer(HttpUser):
    """Simulates a chat viewer: connects to /chat/feed and holds the
    stream open, consuming frames as they arrive. Each Locust user maps
    to one open SSE connection."""

    # Each viewer connects once and stays connected.
    wait_time = between(1, 1)

    def on_start(self):
        # Log in once so this user has a session cookie.
        # The /login form takes a username field, posts it, and Coolify
        # sets a Set-Cookie on the response that requests will preserve.
        self.client.post(
            "/login",
            data={"username": f"loadtest-{id(self)}"},
            allow_redirects=False,
            name="/login",
        )

    @task
    def watch_feed(self):
        # stream=True keeps the response open instead of buffering the
        # whole body. We then read chunks until the connection drops or
        # the test ends.
        start = time.monotonic()
        bytes_read = 0
        with self.client.get(
            "/chat/feed",
            stream=True,
            headers={"Accept": "text/event-stream"},
            name="/chat/feed [stream]",
            timeout=300,
        ) as r:
            for chunk in r.iter_content(chunk_size=4096):
                if not chunk:
                    break
                bytes_read += len(chunk)
                # If we've been connected for 60s, drop and reconnect —
                # this stops one user hogging a connection forever and
                # gives Locust meaningful "request completed" stats.
                if time.monotonic() - start > 60:
                    break
        # Record a synthetic "stream-bytes" metric so you can see the
        # bandwidth each viewer is consuming.
        events.request.fire(
            request_type="STREAM",
            name="bytes-received",
            response_time=int((time.monotonic() - start) * 1000),
            response_length=bytes_read,
            exception=None,
            context={},
        )


class Writer(HttpUser):
    """A small number of users that actually post messages — this is
    what wakes every viewer's parked thread and causes fan-out."""

    wait_time = between(2, 5)  # send a message every 2-5 seconds

    def on_start(self):
        self.client.post(
            "/login",
            data={"username": f"writer-{id(self)}"},
            allow_redirects=False,
            name="/login",
        )

    @task
    def say(self):
        # Datastar POSTs a JSON body with a 'datastar' top-level key
        # containing the signal values. Your post_say handler reads
        # 'text' from the signals.
        self.client.post(
            "/chat/say",
            json={"datastar": {"text": f"hello from locust {time.time():.0f}"}},
            name="/chat/say",
        )
