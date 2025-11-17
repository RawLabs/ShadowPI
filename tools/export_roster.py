from telethon import TelegramClient
from telethon.tl.functions.contacts import ResolveUsernameRequest

api_id =  35161549          # your Telegram API ID from my.telegram.org
api_hash = "4286755dc78d45b1e884c7ce4c506cf8"     # your API hash
chat = "https://t.me/+GDxS0AMtNDU2ZDIx"  # @username or invite link minus https://t.me/

client = TelegramClient("shadowpi-roster", api_id, api_hash)

async def main():
    # Resolve chat if you only have an @username
    entity = await client.get_entity(chat)
    async for member in client.iter_participants(entity):
        if member.bot:
            continue
        user_id = member.id
        username = f" @{member.username}" if member.username else ""
        name = " ".join(filter(None, [member.first_name, member.last_name])) or ""
        print(f"{user_id}{username} {name}".strip())

with client:
    client.loop.run_until_complete(main())
