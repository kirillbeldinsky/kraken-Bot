# 1. Получаем свечи с биржи вашей функцией get_ohlc()
raw_ohlc = get_ohlc(KRAKEN_SYMBOL, TIMEFRAME, limit=500)

# 2. Собираем очищенный DataFrame и подтягиваем bot_state.json
df_candles = build_candles_dataframe(
    raw_ohlc,
    state_path="bot_state.json",
    save_csv_path="candles_sanitized.csv",  # можно убрать, если сохранять файл не нужно
)

print(df_candles.tail())
print("Контекст из JSON:", df_candles.attrs.get("bot_state"))

# 3. Запускаем Walk-Forward (если задать слишком большие train_bars/test_bars для короткой истории,
# уменьшите их, например до train_bars=50, test_bars=20)
wf_report = walk_forward_bb(df_candles, train_bars=50, test_bars=20, fee_pct=FEE_PCT)
print(wf_report)