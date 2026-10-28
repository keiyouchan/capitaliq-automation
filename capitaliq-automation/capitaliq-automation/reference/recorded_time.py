import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page.goto("https://www.capitaliq.spglobal.com/web/client?auth=inherit#office/screener?perspective=127420")
    page.get_by_role("button", name="Commercial Bank, Savings Bank").click()
    page.get_by_role("option", name="Bank Holding Company ").click()
    page.get_by_role("option", name="Savings & Loan Holding").click()
    page.get_by_role("option", name="Savings & Loan Association ").click()
    page.get_by_role("option", name="Credit Union ").click()
    page.locator(".grid > div:nth-child(2)").click()
    page.get_by_role("button", name="All Companies   ").click()
    page.locator(".grid > div:nth-child(2)").click()
    page.get_by_role("button", name="Operating Companies   ").click()
    page.get_by_role("button", name="Select All").click()
    page.locator(".grid > div:nth-child(2)").click()
    page.get_by_role("button", name="Companies   ", exact=True).click()
    page.get_by_role("option", name="Aggregates ").click()
    page.locator(".grid > div:nth-child(2)").click()
    page.get_by_role("button", name="Run Screen").click()
    page.get_by_role("button", name="Display Columns").click()
    page.locator(".jstree-icon").first.click()
    page.locator("[id=\"127461\"] > .jstree-icon.jstree-ocl").click()
    page.locator("[id=\"127461\"] > i").click()
    page.locator("[id=\"127456\"] > .jstree-icon.jstree-ocl").click()
    page.locator("[id=\"127456\"] > i").click()
    page.locator("[id=\"127455\"] > .jstree-icon.jstree-ocl").click()
    page.locator("[id=\"127455\"] > i").click()
    page.locator("[id=\"127461\"] > .jstree-icon.jstree-ocl").click()
    page.locator("[id=\"127456\"] > .jstree-icon.jstree-ocl").click()
    page.get_by_role("treeitem", name="Population", exact=True).click()
    page.get_by_role("button", name="Proj Open menu").click()
    page.get_by_role("option", name="2025").click()
    page.get_by_role("option", name="2024").click()
    page.get_by_role("option", name="2023").click()
    page.locator("[id=\"2022\"] > .css-142u1gn > .css-1feabgd").click()
    page.get_by_role("option", name="Proj").click()
    page.get_by_role("option", name="Proj").get_by_label("", exact=True).check()
    page.get_by_role("button", name="icon-only-button").first.click()
    page.get_by_test_id("apply").click()
    page.get_by_role("button", name="Export").click()
    with page.expect_download() as download_info:
        with page.expect_popup() as page1_info:
            page.get_by_role("link", name="Results are ready for").click()
        page1 = page1_info.value
    download = download_info.value
    page1.close()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
