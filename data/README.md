# Test Data

This directory contains generated test data for development and testing.

## Generating Data

```bash
# Generate with default seed (42)
python3 tools/data_generator.py --output-dir ./test_data

# Generate with specific seed for reproducibility
python3 tools/data_generator.py --seed 42 --output-dir ./test_data

# Print a random seed for later reproduction
python3 tools/data_generator.py --print-seed
```

## Deterministic Output

When the same seed and arguments are provided, the output is byte-for-byte identical:

```bash
python3 tools/data_generator.py --seed 42 --output-dir ./run1
python3 tools/data_generator.py --seed 42 --output-dir ./run2
diff <(find ./run1 -name "*.json" -exec md5sum {} \;) <(find ./run2 -name "*.json" -exec md5sum {} \;)
# No output = identical
```

## Output Files

- `users.json` - User accounts
- `orders.json` - Trading orders
- `trades.json` - Executed trades
- `ticks.json` - Price ticks per instrument
- `candles.json` - OHLCV candles per instrument
- `instruments.json` - Instrument definitions
- `metadata.json` - Generation metadata (seed, timestamp)
