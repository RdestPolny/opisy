"""Description-only workflow; no UI, secrets, database or model selection."""
import random
import time

import httpx
import requests

from description_output import VALIDATOR_API_VERSION, analyze_description_html


def _retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return code in {408, 429, 500, 502, 503, 504} or isinstance(
        exc, (TimeoutError, ConnectionError, httpx.TimeoutException,
              httpx.NetworkError, requests.Timeout, requests.ConnectionError)
    )


def _retry_delay(exc: Exception, attempt: int) -> float:
    headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
    try:
        return min(max(float(headers["Retry-After"]), 0), 60)
    except (KeyError, TypeError, ValueError):
        return min(2 ** (attempt - 1) + random.uniform(0, 0.5), 30)


def generate_description_result(request, *, contents, context, normalize_response,
                                max_attempts=3, sleep=time.sleep) -> dict:
    """One shared budget, no additional generation for editorial warnings.

    request must perform one SDK attempt (including disabled SDK retries).
    A usable response is returned immediately, so later failures cannot erase it.
    """
    # Fail bad validator configuration before spending the first API request.
    analyze_description_html("", **context)
    if max_attempts < 1:
        raise ValueError("max_attempts musi być dodatnie")
    last_error = "Nie uzyskano opisu"
    previous = ""
    for attempt in range(1, max_attempts + 1):
        prompt = contents
        if previous:
            prompt += "\nPoprzedni wynik do naprawy:\n" + previous
        if attempt > 1:
            prompt += "\nPoprzednia próba: " + last_error + ". Zwróć kompletny, niepusty opis HTML."
        try:
            response = request(prompt)
        except Exception as exc:
            last_error = f"Błąd Gemini: {exc}"
            if not _retryable(exc) or attempt == max_attempts:
                break
            sleep(_retry_delay(exc, attempt))
            continue

        raw = normalize_response(response.text or "")
        report = analyze_description_html(raw, **context)
        candidates = getattr(response, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", None) if candidates else None
        finish = str(getattr(finish, "value", finish) or "")
        previous = report.clean_html
        if not report.errors:
            generation_warnings = [issue for issue in report.warnings if issue["code"] == "HTML_NORMALIZED"]
            if finish and finish != "STOP":
                generation_warnings.append({
                    "code": "OUTPUT_MAY_BE_TRUNCATED",
                    "message": f"Zachowano otrzymany opis; powód zakończenia generowania: {finish}. Sprawdź kompletność.",
                })
            warnings = report.warnings + [issue for issue in generation_warnings if issue not in report.warnings]
            return {
                "description_html": report.clean_html,
                "error": None,
                "status": "completed_with_warnings" if warnings else "completed",
                "validation_errors": [],
                "validation_warnings": warnings,
                "generation_warnings": generation_warnings,
                "validation_version": VALIDATOR_API_VERSION,
                "api_attempts": attempt,
                "finish_reason": finish,
            }
        last_error = "; ".join(report.errors)
        # Do not repeat a provider-blocked request with the same contents.
        if finish and finish not in {"STOP", "MAX_TOKENS"}:
            break
    return {
        "description_html": previous,
        "error": last_error,
        "status": "error",
        "validation_errors": [last_error],
        "validation_warnings": [],
        "generation_warnings": [],
        "validation_version": VALIDATOR_API_VERSION,
        "api_attempts": attempt,
    }
