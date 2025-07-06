import asyncio

class ConcurrencyManager:
    async def run_task(self, task):
        return await task()
