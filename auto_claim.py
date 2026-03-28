"""
auto_claim.py — Playwright-based auto-claimer for Polymarket winnings.

Opens Polymarket portfolio page, finds claimable positions, and clicks
the claim/redeem buttons. Runs headless by default.

Usage:
    python auto_claim.py [--headed]
"""

import argparse
import sys
import time


def run_claim(headed=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: playwright not installed. Run: pip install playwright && playwright install")
        sys.exit(1)

    print("[auto_claim] Starting browser...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context()
        page = context.new_page()

        # Navigate to Polymarket portfolio
        page.goto("https://polymarket.com/portfolio", wait_until="networkidle", timeout=30000)
        time.sleep(3)

        # Check if we need to connect wallet
        if page.query_selector("text=Connect Wallet"):
            print("[auto_claim] Wallet not connected. Please connect manually in headed mode.")
            if not headed:
                print("[auto_claim] Re-run with --headed to connect wallet interactively.")
                browser.close()
                return

            print("[auto_claim] Waiting 60s for manual wallet connection...")
            page.wait_for_selector("text=Portfolio", timeout=60000)
            time.sleep(2)

        # Look for claimable/redeemable positions
        claim_buttons = page.query_selector_all("button:has-text('Claim'), button:has-text('Redeem')")

        if not claim_buttons:
            print("[auto_claim] No claimable positions found.")
            browser.close()
            return

        print(f"[auto_claim] Found {len(claim_buttons)} claimable position(s)")

        for i, btn in enumerate(claim_buttons):
            try:
                btn_text = btn.inner_text()
                print(f"[auto_claim] Clicking: {btn_text} ({i+1}/{len(claim_buttons)})")
                btn.click()
                time.sleep(3)

                # Handle confirmation dialog if present
                confirm = page.query_selector("button:has-text('Confirm')")
                if confirm:
                    confirm.click()
                    time.sleep(5)

                print(f"[auto_claim] Claimed {i+1}/{len(claim_buttons)}")
            except Exception as e:
                print(f"[auto_claim] Error claiming position {i+1}: {e}")

        print("[auto_claim] Done claiming.")
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-claim Polymarket winnings")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser")
    args = parser.parse_args()
    run_claim(headed=args.headed)
