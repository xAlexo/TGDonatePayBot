from asyncio import sleep
import logging

import sentry_sdk
from telethon import TelegramClient, events
from motor.motor_asyncio import AsyncIOMotorClient
from telethon.tl.custom import Message
from aiohttp import ClientSession

from config import MONGODB_URI, SENTRY_DSN, TG_API_HASH, TG_API_ID, \
    TG_BOT_API_TOKEN
from contrib.status import Status


logging.basicConfig(level=logging.ERROR)

bot = TelegramClient('TGDonatePayBot', TG_API_ID, TG_API_HASH)
bot.start(bot_token=TG_BOT_API_TOKEN)
bot.db = AsyncIOMotorClient(MONGODB_URI).tg_donate_pay_bot

sentry_sdk.init(SENTRY_DSN)


@bot.on(events.NewMessage(pattern='/start'))
async def start(event: Message):
    db = event.client.db
    await db.chat.find_one_and_update(
        {'_id': event.chat_id},
        {'$set': {'connections': []}},
        upsert=True,
        return_document=True,
    )
    await event.reply(
        'Привет! Я могу пересылать сообщения из donatepay в канал '
        'telegram. Для создания нового перенаправления введите команду '
        '/new_connection')
    raise events.StopPropagation()


@bot.on(events.NewMessage(pattern='/new_connection'))
async def new_connection(event):
    db = event.client.db

    await event.respond(
        'Для начала создайте канал (приватный тоже подходит), '
        'добавьте меня в админы канала (с правами публикации сообщений), '
        'и перешлите мне сюда любое сообщение из канала')
    await db.chat.update_one(
        {'_id': event.chat_id},
        {'$set': {'status': Status.WAIT_CHANNEL}},
    )
    raise events.StopPropagation()


@bot.on(events.NewMessage())
async def default(event):
    db = event.client.db
    chat = await db.chat.find_one({'_id': event.chat_id})
    if not chat:
        await event.respond('Сначала введите команду /start')
        return

    try:
        if chat.get('status', 3) == Status.WAIT_CHANNEL:
            if not event.forward or not event.forward.chat_id:
                await event.respond('Это не пересланное сообщение!')
                return

            try:
                msg = await bot.send_message(
                    event.forward.chat_id,
                    'Тестовая запись!',
                )
                chat = await db.chat.find_one_and_update(
                    {
                        '_id': event.chat_id,
                        'connections.channel_id': {'$ne': event.forward.chat_id}
                    },
                    {
                        '$set': {
                            'status': Status.WAIT_DP_API_KEY,
                            'wait_dp_api': event.forward.chat_id
                        },
                        '$push': {
                            'connections': {
                                'channel_id': event.forward.chat_id
                            }
                        }
                    },
                    return_document=True,
                )
                await bot.delete_messages(event.forward.chat_id, msg)
                if not chat:
                    await db.chat.update_one(
                        {'_id': event.chat_id},
                        {'$set': {'status': Status.WAIT_DP_API_KEY}}
                    )
                    return await event.respond(
                        'Проблема с добавлением коннекта')

                return await event.respond(
                        'Теперь необходимо ввести API ключ от donatepay.ru\n'
                        'Взять его можно тут:\n'
                        'https://donatepay.ru/page/api\n'
                        'поле "Ваш API ключ"\n'
                        '\n'
                        'ВНИМАНИЕ: С помощью API нельзя вывести деньги, '
                        'на странице получения ключа видны все методы, '
                        'их 3:\n'
                        'получение информации об аккаунте;\n'
                        'получение информации о транзакциях;\n'
                        'создание транзакции с оповещением '
                        '(донаты с озвучкой)')
            except Exception as e:
                event.respond('Что то не так с пересланным сообщением')
                logging.exception(e)

        if chat.get('status') == Status.WAIT_DP_API_KEY:
            url = f'https://donatepay.ru/api/v1/user?access_token={event.text}'
            async with ClientSession() as cs:
                async with cs.get(url) as r:
                    if r.status != 200:
                        return
            chat = await db.chat.find_one_and_update(
                {
                    '_id': event.chat_id,
                    'connections.channel_id': chat['wait_dp_api']
                },
                {
                    '$set': {
                        'status': Status.NOT_SET,
                        'connections.$.dp_api_key': event.text,
                    },
                    '$unset': {
                        'wait_dp_api': 1,
                    }
                }
            )
            if not chat:
                return await event.respond('Что то пошло не так!')

            return await event.respond(
                'Всё настроено, теперь новые донаты будут приходить в '
                'канал!')
    except Exception as e:
        logging.exception(e)


async def check_donate_pay(bot):
    url = 'https://donatepay.ru/api/v1/transactions'
    while True:
        try:
            db = bot.db
            async for chat in db.chat.aggregate([{'$unwind': '$connections'}]):
                channel_id = chat['connections']['channel_id']
                db_api_key = chat['connections']['dp_api_key']
                last_donation = chat['connections'].get('last_donation')
                params = {
                    'access_token': db_api_key,
                    'type': 'donation',
                    'order': 'ASC',
                    'status': 'success',
                }
                if last_donation:
                    params['after'] = last_donation

                async with ClientSession() as cs:
                    async with cs.get(url, params=params) as r:
                        if r.status != 200:
                            continue

                        data = await r.json()
                        last = None
                        for d in data.get('data', []):
                            await bot.send_message(
                                channel_id,
                                f'Имя: {d["what"]}\n'
                                f'Сумма: {d["sum"]}\n'
                                f'\n'
                                f'{d["comment"]}'
                            )
                            last = d

                        if last:
                            await db.chat.update_one(
                                {
                                    '_id': chat['_id'],
                                    'connections.channel_id': channel_id
                                },
                                {
                                    '$set': {
                                        'connections.$.last_donation': last['id']
                                    }
                                }
                            )
        except Exception as e:
            logging.exception(e)

        await sleep(30)


if __name__ == '__main__':
    bot.loop.create_task(check_donate_pay(bot))
    bot.run_until_disconnected()
