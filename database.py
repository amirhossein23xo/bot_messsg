import motor.motor_asyncio
from config import MONGO_URI


class Database:
    def __init__(self):
        self.client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        self.db = self.client["bio_watcher"]
        self.settings = self.db["settings"]
        self.users = self.db["monitored_users"]

    # ---- log channel ----
    async def get_log_channel(self):
        doc = await self.settings.find_one({"_id": "log_channel"})
        return doc["channel_id"] if doc else None

    async def set_log_channel(self, channel_id: int):
        await self.settings.update_one(
            {"_id": "log_channel"},
            {"$set": {"channel_id": channel_id}},
            upsert=True,
        )

    # ---- monitored users ----
    async def get_all_monitored(self):
        cursor = self.users.find({})
        return [doc async for doc in cursor]

    async def is_monitored(self, user_id: int, chat_id: int) -> bool:
        doc = await self.users.find_one({"user_id": user_id, "chat_id": chat_id})
        return doc is not None

    async def add_monitored(self, user_id: int, chat_id: int):
        await self.users.update_one(
            {"user_id": user_id, "chat_id": chat_id},
            {"$setOnInsert": {"last_links": []}},
            upsert=True,
        )

    async def remove_monitored(self, user_id: int, chat_id: int):
        await self.users.delete_one({"user_id": user_id, "chat_id": chat_id})

    async def get_last_links(self, user_id: int, chat_id: int):
        doc = await self.users.find_one({"user_id": user_id, "chat_id": chat_id})
        return doc.get("last_links", []) if doc else []

    async def update_last_links(self, user_id: int, chat_id: int, links: list):
        await self.users.update_one(
            {"user_id": user_id, "chat_id": chat_id},
            {"$set": {"last_links": links}},
            upsert=True,
        )

    # ---- links ever sent to the log channel (for new/duplicate detection) ----
    async def get_sent_links(self, user_id: int, chat_id: int):
        doc = await self.users.find_one({"user_id": user_id, "chat_id": chat_id})
        return doc.get("sent_links", []) if doc else []

    async def add_sent_links(self, user_id: int, chat_id: int, links: list):
        if not links:
            return
        await self.users.update_one(
            {"user_id": user_id, "chat_id": chat_id},
            {"$addToSet": {"sent_links": {"$each": links}}},
            upsert=True,
        )
