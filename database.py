import motor.motor_asyncio
from config import MONGO_URI


class Database:
    def __init__(self):
        self.client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        self.db = self.client["bio_watcher"]
        self.settings = self.db["settings"]
        self.users = self.db["monitored_users"]
        self.user_links = self.db["user_sent_links"]  # global, per-user, across all groups

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

    # ---- links ever sent to the log channel (global per user, across ALL
    # groups they're monitored in) — this is what decides "جدید" vs "تکراری" ----
    async def get_sent_links(self, user_id: int):
        doc = await self.user_links.find_one({"user_id": user_id})
        return doc.get("sent_links", []) if doc else []

    async def add_sent_links(self, user_id: int, links: list):
        if not links:
            return
        await self.user_links.update_one(
            {"user_id": user_id},
            {"$addToSet": {"sent_links": {"$each": links}}},
            upsert=True,
        )

    async def claim_link(self, user_id: int, link: str) -> bool:
        """Atomically mark `link` as sent for this user, but ONLY if it
        hasn't been claimed before.

        Returns True  -> this call just became the first to send it (go ahead, send it).
        Returns False -> someone (this process or another one) already sent it before;
                          do NOT send it again.

        This is done as a single atomic MongoDB operation (find-and-update with
        a "link not already in the array" filter) specifically so that even if
        two instances of the bot end up running at the same time (e.g. an old
        Railway deployment left running alongside a new one), only ONE of them
        can ever win the race and actually send a given link. A plain
        read-then-write (get_sent_links + add_sent_links) can NOT guarantee
        this, because two processes can both "read: not sent yet" before
        either one "writes: now sent".
        """
        result = await self.user_links.update_one(
            {"user_id": user_id, "sent_links": {"$ne": link}},
            {"$addToSet": {"sent_links": link}},
            upsert=True,
        )
        # matched_count == 1  -> doc existed and didn't have this link -> we just added it -> WE claimed it
        # upserted_id is set  -> doc didn't exist at all yet -> we just created it with this link -> WE claimed it
        # matched_count == 0 and upserted_id is None -> doc existed AND already had this link -> already claimed by someone else
        return result.matched_count == 1 or result.upserted_id is not None
