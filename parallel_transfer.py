import math
import os
import asyncio
import io
import time
from telethon.tl.functions.upload import GetFileRequest, SaveBigFilePartRequest, SaveFilePartRequest
from telethon.tl.types import InputFile, InputFileBig

from telethon.errors import FloodWaitError, ServerError, RpcCallFailError

async def fast_download_file(client, location, file_size, workers=4, part_size_kb=512, max_retries=2):
    part_size = part_size_kb * 1024
    part_count = (file_size + part_size - 1) // part_size
    buffer = bytearray(file_size)

    sem = asyncio.Semaphore(workers)

    async def download_part(part_index):
        offset = part_index * part_size
        req = GetFileRequest(location, offset=offset, limit=part_size)

        for attempt in range(max_retries):
            try:
                async with sem:
                    result = await client(req)
                    buffer[offset:offset + len(result.bytes)] = result.bytes
                    return
            except FloodWaitError as fwe:
                await asyncio.sleep(fwe.seconds + 2)
            except (ServerError, RpcCallFailError, ConnectionError, asyncio.TimeoutError, Exception) as e:
                if attempt == max_retries - 1:
                    raise e
                sleep_time = min(10, (2 ** attempt) + 1)
                await asyncio.sleep(sleep_time)

    tasks = [download_part(i) for i in range(part_count)]
    await asyncio.gather(*tasks)
    return io.BytesIO(buffer)

async def fast_upload_file(client, file_data, file_name, workers=4, part_size_kb=512, max_retries=2):
    file_size = len(file_data)
    part_size = part_size_kb * 1024
    part_count = (file_size + part_size - 1) // part_size
    import telethon.helpers
    is_big = file_size > 10 * 1024 * 1024
    file_id = telethon.helpers.generate_random_long()

    sem = asyncio.Semaphore(workers)

    async def upload_part(part_index):
        offset = part_index * part_size
        chunk = file_data[offset:offset + part_size]
        if is_big:
            req = SaveBigFilePartRequest(file_id, part_index, part_count, chunk)
        else:
            req = SaveFilePartRequest(file_id, part_index, chunk)

        for attempt in range(max_retries):
            try:
                async with sem:
                    await client(req)
                    return
            except FloodWaitError as fwe:
                await asyncio.sleep(fwe.seconds + 2)
            except (ServerError, RpcCallFailError, ConnectionError, asyncio.TimeoutError, Exception) as e:
                if attempt == max_retries - 1:
                    raise e
                sleep_time = min(30, (2 ** attempt) + 1)
                await asyncio.sleep(sleep_time)

    tasks = [upload_part(i) for i in range(part_count)]
    await asyncio.gather(*tasks)

    if is_big:
        return InputFileBig(file_id, part_count, file_name)
    else:
        return InputFile(file_id, part_count, file_name, "")

