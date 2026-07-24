# Импорт необходимых библиотек
import pandas as pd

# Загрузка данных
data = pd.read_csv('historical_data.csv')

# Определение параметров стратегии
stop_loss = 0.015
take_profit = 0.025

# Логика стратегии
def backtest_strategy(data):
    signals = []
    for i in range(1, len(data)):
        # Пример условия входа
        if data['Close'][i] > data['Close'][i-1]*1.01:
            signals.append('buy')
        elif data['Close'][i] < data['Close'][i-1]*0.99:
            signals.append('sell')
        else:
            signals.append('hold')
    return signals

# Применение стратегии
result_signals = backtest_strategy(data)

# Вывод результатов
print(result_signals)

