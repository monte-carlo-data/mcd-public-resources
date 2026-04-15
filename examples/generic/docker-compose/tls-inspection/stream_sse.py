"""mitmproxy addon: stream only SSE responses to avoid buffering long-lived connections."""


def responseheaders(flow):
    content_type = flow.response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        flow.response.stream = True
