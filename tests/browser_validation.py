"""Explicit real-browser regression check; run separately from component tests."""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from offline_snapshot.archive import entry, new_archive
from offline_snapshot.capture import validate
from offline_snapshot.pack import pack


async def main():
    archive = new_archive("https://example.org/")
    archive["entries"] = [
        entry(
            archive["url"],
            b'<html><body><h1>Actual page</h1><script>console.error("diagnostic sentinel")</script></body></html>',
            "text/html",
        )
    ]
    output = Path("outputs/validator-regression.html")
    pack(archive, output)
    recipe = Path("reports/validator-regression-recipe.json")
    recipe.write_text(
        json.dumps(
            {
                "expect_text": "Missing required text",
                "steps": [{"action": "click", "selector": "#absent", "checkpoint": "dependent"}],
            }
        )
    )
    result = await validate(output, "chromium", recipe, "reports/validator-regression.json")
    Path("reports/validator-regression.json").write_text(json.dumps(result, indent=2))
    assert not result["passed"]
    assert result["checks"][0]["status"] == "failed"
    assert result["checks"][1]["status"] == "not_run"
    assert "diagnostic sentinel" in result["consoleErrors"]
    assert result["runtimeReport"]["misses"] == []
    assert Path(result["checkpoints"][0]["screenshot"]).is_file()
    assert result["environment"]["offline"]
    assert result["networkAttempts"] == []

    visual_archive = new_archive("https://example.org/visual")
    visual_archive["pages"] = [{"url": visual_archive["url"]}]
    visual_body = b"<html><body><h1>Visual sentinel</h1></body></html>"
    visual_archive["entries"] = [entry(visual_archive["url"], visual_body, "text/html")]
    visual_output = Path("outputs/validator-visual.html")
    pack(visual_archive, visual_output)
    baseline = Path("reports/validator-visual-baseline.png")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(chromium_sandbox=True)
        context = await browser.new_context(
            viewport={"width": 1363, "height": 936}, locale="en-US", timezone_id="UTC"
        )
        page = await context.new_page()
        await page.set_content(visual_body.decode())
        await page.screenshot(path=baseline)
        await context.close()
        await browser.close()
    visual_recipe = Path("reports/validator-visual-recipe.json")
    visual_recipe.write_text(json.dumps({"expect_text": "Visual sentinel"}))
    visual_result = await validate(
        visual_output,
        "chromium",
        visual_recipe,
        "reports/validator-visual.json",
        visual_baseline=baseline,
    )
    Path("reports/validator-visual.json").write_text(json.dumps(visual_result, indent=2))
    assert visual_result["passed"], visual_result["visualChecks"]
    from PIL import Image, ImageChops

    appearance = Image.open(visual_result["visualChecks"][0]["actual"]).convert("RGB")
    corner = appearance.crop((0, 880, 140, 936))
    assert ImageChops.difference(corner, Image.new("RGB", corner.size, "white")).getbbox() is None
    print(
        "PASS: failure evidence is retained and opaque-frame visual comparison excludes only the diagnostic panel."
    )


if __name__ == "__main__":
    asyncio.run(main())
