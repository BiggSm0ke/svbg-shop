# Платежи: как подключить кассу

Касса в боте называется инстансом провайдера. Его настройки лежат в ключах `PAY_<SLUG>_*`, где SLUG — имя провайдера большими буквами, например `PAY_ROLLYPAY_API_KEY`. Менять их можно в боте (`/settings` → Платёжки) или с сервера: `svbg set KEY VALUE`. Секреты хранятся зашифрованными, в логи не попадают.

## Подключение

1. Возьмите ключи в кабинете кассы. Какие именно, написано в `docs/providers/<slug>.md`, в разделе «Настройки инстанса».
2. Проверьте, что задан `PUBLIC_URL` (публичный https-адрес бота). Без него бот не сможет построить адрес вебхука.
3. Введите ключи. Бот проверит их пробным запросом к кассе, счёт при этом не создаётся.
4. Включите кассу: `PAY_<SLUG>_ENABLED=true`. После этого в разделе кассы появится адрес вебхука.
5. Вставьте этот адрес в кабинете кассы (обычно поле называется «URL уведомлений»).
6. Сделайте тестовый платёж на минимальную сумму.

## Адрес вебхука

```
<PUBLIC_URL>/webhooks/pay/{instance}/{token}
```

`{instance}` — номер инстанса, `{token}` — его секретный токен. Токен генерирует бот и хранит зашифрованным, так что адрес никому не показывайте. Подпись вебхука бот проверяет отдельно: на неверную отвечает 401, устаревшие события и повторы отбрасывает. Если касса принимает адрес уведомлений прямо в запросе на создание счёта, бот передаёт его сам и в кабинете ничего вписывать не нужно.

## Прокси для кассы

Бывает, что бот стоит за границей, а касса не пускает зарубежные IP. Тогда задайте прокси только этому инстансу:

```
svbg set PAY_<SLUG>_PROXY_URL socks5://user:pass@host:1080
```

Подходят схемы `http://`, `https://`, `socks4://`, `socks5://`, `socks5h://`. Пустое значение — без прокси. Адрес прокси хранится как секрет. Через прокси идут только запросы бота к кассе, входящие вебхуки он не затрагивает.

## Тестовый режим

С `PAY_<SLUG>_TEST_MODE=true` инстанс работает с тестовым контуром кассы и принимает только тестовые события, боевые отклоняет. В обычном режиме наоборот. Не забудьте выключить после проверки.

## Провайдеры (27)

| Slug | Методы | Описание |
|---|---|---|
| antilopay | карта, СБП | [antilopay.md](../providers/antilopay.md) |
| aurapay | СБП, карта | [aurapay.md](../providers/aurapay.md) |
| cispay | СБП, карта | [cispay.md](../providers/cispay.md) |
| cloudpayments | карта, СБП | [cloudpayments.md](../providers/cloudpayments.md) |
| cryptobot | крипта | [cryptobot.md](../providers/cryptobot.md) |
| cryptomus | крипта | [cryptomus.md](../providers/cryptomus.md) |
| etoplatezhi | СБП, карта | [etoplatezhi.md](../providers/etoplatezhi.md) |
| freekassa | карта, СБП, крипта | [freekassa.md](../providers/freekassa.md) |
| heleket | крипта | [heleket.md](../providers/heleket.md) |
| lava | карта, СБП | [lava.md](../providers/lava.md) |
| manual | ручной (перевод, подтверждает админ) | [manual.md](../providers/manual.md) |
| mulenpay | карта, СБП | [mulenpay.md](../providers/mulenpay.md) |
| overpay | карта, СБП | [overpay.md](../providers/overpay.md) |
| pal24 | карта, СБП | [pal24.md](../providers/pal24.md) |
| paritypay | СБП, карта | [paritypay.md](../providers/paritypay.md) |
| paypear | СБП, карта | [paypear.md](../providers/paypear.md) |
| platega | СБП, карта, зарубежная карта, крипта | [platega.md](../providers/platega.md) |
| riopay | СБП, карта | [riopay.md](../providers/riopay.md) |
| robokassa | карта, СБП | [robokassa.md](../providers/robokassa.md) |
| rollypay | СБП | [rollypay.md](../providers/rollypay.md) |
| severpay | СБП, карта | [severpay.md](../providers/severpay.md) |
| stars | Telegram Stars | [stars.md](../providers/stars.md) |
| tabpay | СБП, карта | [tabpay.md](../providers/tabpay.md) |
| tribute | карта | [tribute.md](../providers/tribute.md) |
| wata | карта, СБП | [wata.md](../providers/wata.md) |
| yookassa | СБП, карта | [yookassa.md](../providers/yookassa.md) |
| yoomoney | карта, кошелёк | [yoomoney.md](../providers/yoomoney.md) |

Методы перечислены как в манифесте плагина, первый считается основным. Если нужна вторая касса того же провайдера, заведите второй инстанс с другим SLUG.

В файле каждой кассы есть всё, что нужно для подключения: какие ключи взять в кабинете, куда вставить вебхук, как устроена проверка и что смотреть, если платежи не доходят.

## Кассы, которых пока нет

Для Cashera, Jupiter (FPGate) и Donut плагинов нет: у них нет открытой документации API, а писать протокол наугад нельзя. Что запросить у каждой и какие риски у P2P-процессинга, написано в [cashera.md](../providers/cashera.md), [jupiter.md](../providers/jupiter.md) и [donut.md](../providers/donut.md).
