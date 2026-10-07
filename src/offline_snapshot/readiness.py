"""Explicit, non-clicking readiness checks shared by capture and replay tests."""

import asyncio
import copy
import json
from pathlib import Path


class ReadinessError(RuntimeError):
    def __init__(self, evidence):
        self.evidence = evidence
        super().__init__(f"Page readiness failed for {evidence['selector']}: {evidence['error']}")


def read_recipe(path=None, ready_selector=None):
    recipe = (
        copy.deepcopy(path)
        if isinstance(path, dict)
        else (json.loads(Path(path).read_text(encoding="utf-8")) if path else {})
    )
    if not isinstance(recipe, dict):
        raise ValueError("Recipe must be a JSON object")
    if ready_selector is not None:
        recipe["ready"] = ready_selector
    return recipe


# Playwright visibility includes opacity:0. Check ancestors, including shadow
# hosts, before the pointer test. This is a bounded assertion, not a complete
# pixel-visibility or challenge detector.
OPAQUE = """el => {
  for (let node=el; node; node=node.parentElement || node.getRootNode()?.host) {
    if (Number(getComputedStyle(node).opacity) === 0) return false;
  }
  return true;
}"""


async def check_ready(page, spec, *, timeout=15000):
    selector = spec.get("ready")
    if not selector:
        return {
            "status": "not_run",
            "reason": "No ready selector supplied; document load is not a usability assertion.",
        }
    duration = spec.get("ready_timeout_ms", timeout)
    if not isinstance(duration, (int, float)) or not 0 < duration < float("inf"):
        raise ValueError("ready_timeout_ms must be a positive finite number")
    evidence = {
        "selector": selector,
        "status": "passed",
        "method": "visible, nontransparent ancestors, stable, enabled, receives pointer; no click sent",
    }
    deadline = asyncio.get_running_loop().time() + duration / 1000
    target = page.locator(selector)
    try:
        await target.wait_for(state="visible", timeout=duration)
        while True:
            remaining = (deadline - asyncio.get_running_loop().time()) * 1000
            if remaining <= 0:
                raise TimeoutError("Target or an ancestor remained fully transparent")
            if await target.evaluate(OPAQUE, timeout=remaining):
                break
            await asyncio.sleep(min(0.05, remaining / 1000))
        await target.click(
            trial=True, timeout=max(1, (deadline - asyncio.get_running_loop().time()) * 1000)
        )
        if not await target.evaluate(OPAQUE):
            raise AssertionError("Target became fully transparent during the readiness check")
    except Exception as exc:
        evidence.update(status="failed", error=str(exc)[:4000])
        raise ReadinessError(evidence) from exc
    return evidence
