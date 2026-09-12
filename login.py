from telethon import TelegramClient

API_ID = 12345678  # Your API ID
API_HASH = "your_telegram_api_hash"  # Your API Hash

client = TelegramClient("vault_session", API_ID, API_HASH)

async def main():
    print("Logged in successfully!")

with client:
    client.loop.run_until_complete(main())

