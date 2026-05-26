import json

import requests


class JsonHttpRequestError(Exception):
    pass


def post_json(url, payload, headers=None, timeout=45):
    try:
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        response = requests.post(
            url,
            headers=request_headers,
            json=payload,
            timeout=float(timeout or 45),
        )
        response.raise_for_status()
        response_body = response.text
    except requests.exceptions.HTTPError as exc:
        response = getattr(exc, "response", None)
        response_body = response.text if response is not None else ""
        status_code = response.status_code if response is not None else "HTTPError"
        raise JsonHttpRequestError("HTTP %s: %s" % (status_code, response_body or exc)) from exc
    except requests.exceptions.RequestException as exc:
        raise JsonHttpRequestError("Connection failed: %s" % exc) from exc

    try:
        return json.loads(response_body or "{}")
    except json.JSONDecodeError as exc:
        raise JsonHttpRequestError("Invalid JSON response: %s" % exc) from exc
