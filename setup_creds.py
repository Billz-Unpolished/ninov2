"""
setup_creds.py — One-time setup to derive Polymarket API credentials from your private key.

Usage:
    python setup_creds.py

Requires POLY_PRIVATE_KEY in .env (or enter interactively).
Writes derived credentials back to .env.
"""

import os
import sys

from dotenv import load_dotenv, set_key

load_dotenv()


def main():
    pk = os.getenv("POLY_PRIVATE_KEY")
    if not pk:
        pk = input("Enter your Polymarket private key (0x...): ").strip()
        if not pk:
            print("No private key provided. Exiting.")
            sys.exit(1)

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        print("Error: py-clob-client not installed. Run: pip install py-clob-client==0.34.5")
        sys.exit(1)

    host = "https://clob.polymarket.com"
    chain_id = 137  # Polygon mainnet

    print("Deriving API credentials...")
    client = ClobClient(host, key=pk, chain_id=chain_id)

    try:
        creds = client.derive_api_key()
    except Exception as e:
        print(f"Error deriving API key: {e}")
        print("Make sure your private key is correct and has a Polymarket account.")
        sys.exit(1)

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    # Ensure .env exists
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write("")

    set_key(env_path, "POLY_PRIVATE_KEY", pk)
    set_key(env_path, "POLY_API_KEY", creds.api_key)
    set_key(env_path, "POLY_API_SECRET", creds.api_secret)
    set_key(env_path, "POLY_API_PASSPHRASE", creds.api_passphrase)

    # Get funder address (proxy wallet)
    try:
        from eth_account import Account
        account = Account.from_key(pk)
        print(f"Wallet address: {account.address}")
        print("NOTE: Your POLY_FUNDER_ADDRESS should be your Polymarket proxy wallet,")
        print("which may differ from your EOA. Check Polymarket settings.")
    except ImportError:
        pass

    print(f"\nCredentials saved to {env_path}")
    print(f"  API Key:        {creds.api_key[:12]}...")
    print(f"  API Secret:     {creds.api_secret[:12]}...")
    print(f"  API Passphrase: {creds.api_passphrase[:12]}...")
    print("\nDon't forget to set POLY_FUNDER_ADDRESS in .env!")
    print("You can find it in your Polymarket account settings (proxy/smart wallet address).")


if __name__ == "__main__":
    main()
