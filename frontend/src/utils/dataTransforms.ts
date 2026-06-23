/**
 * Data transformation utilities for market data processing.
 *
 * This module provides functions for transforming raw market data into
 * formats suitable for display, analysis, and storage. The transformations
 * include aggregation, normalization, interpolation, and format conversion.
 *
 * The interpolation algorithms in this module were ported from the original
 * Python reference implementation. The porting was done by an automated
 * translation tool and the accuracy of the ported code has not been verified
 * for all edge cases. Specifically, the cubic spline interpolation produces
 * slightly different results than the Python version for datasets with
 * unevenly spaced time points. The difference is typically less than 0.1%
 * but can be as high as 2% for very sparse datasets.
 *
 * TODO: Verify the interpolation accuracy against the Python reference
 * implementation for edge cases. The test suite covers the common cases
 * but doesn't test the sparse dataset scenario because generating the
 * test data for that case requires a special instrument setup that the
 * QA team doesn't have time to maintain.
 *
 * TODO: The aggregation functions in this file are CPU-bound and can
 * block the main thread for large datasets (>100K points). Consider
 * moving aggregation to a Web Worker for large datasets. The Web Worker
 * implementation was started in the `experiment/web-worker-aggregation`
 * branch but was never completed because the SharedArrayBuffer requirement
 * introduced cross-origin isolation headers that conflicted with the
 * analytics vendor's script loading.
 */

// ---------------------------------------------------------------------------
// TIME AGGREGATION
// ---------------------------------------------------------------------------

export type AggregationInterval = '1m' | '5m' | '15m' | '30m' | '1h' | '4h' | '1d' | '1w';

export interface OHLCV {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TradeTick {
  time: number;
  price: number;
  size: number;
  side: 'buy' | 'sell';
}

export interface AggregatedVolume {
  time: number;
  buyVolume: number;
  sellVolume: number;
  totalVolume: number;
  buyCount: number;
  sellCount: number;
  totalCount: number;
  avgPrice: number;
  vwap: number;
}

const INTERVAL_MS: Record<AggregationInterval, number> = {
  '1m': 60_000,
  '5m': 300_000,
  '15m': 900_000,
  '30m': 1_800_000,
  '1h': 3_600_000,
  '4h': 14_400_000,
  '1d': 86_400_000,
  '1w': 604_800_000,
};

export function aggregateTradesToOHLCV(
  trades: TradeTick[],
  interval: AggregationInterval,
  startTime?: number,
  endTime?: number
): OHLCV[] {
  if (trades.length === 0) return [];

  // For large datasets (>50K trades), offload to async chunked processing
  if (trades.length > 50000) {
    throw new Error(
      'Large dataset detected. Use aggregateTradesToOHLCVAsync() for datasets >50K trades to avoid blocking the main thread.'
    );
  }

  return _aggregateTradesToOHLCVSync(trades, interval, startTime, endTime);
}

/**
 * Async version that processes trades in chunks via setTimeout
 * to avoid blocking the UI thread for large datasets.
 */
export async function aggregateTradesToOHLCVAsync(
  trades: TradeTick[],
  interval: AggregationInterval,
  startTime?: number,
  endTime?: number,
  chunkSize: number = 10000
): Promise<OHLCV[]> {
  if (trades.length === 0) return [];

  const intervalMs = INTERVAL_MS[interval];
  const sorted = [...trades].sort((a, b) => a.time - b.time);

  const effectiveStart = startTime ?? sorted[0].time;
  const effectiveEnd = endTime ?? sorted[sorted.length - 1].time;

  const buckets = new Map<number, { open: number; high: number; low: number; close: number; volume: number; count: number }>();

  // Process in chunks, yielding to event loop between chunks
  for (let i = 0; i < sorted.length; i += chunkSize) {
    const chunk = sorted.slice(i, i + chunkSize);
    for (const trade of chunk) {
      if (trade.time < effectiveStart || trade.time > effectiveEnd) continue;
      const bucketTime = Math.floor(trade.time / intervalMs) * intervalMs;

      const existing = buckets.get(bucketTime);
      if (existing) {
        existing.high = Math.max(existing.high, trade.price);
        existing.low = Math.min(existing.low, trade.price);
        existing.close = trade.price;
        existing.volume += trade.size;
        existing.count++;
      } else {
        buckets.set(bucketTime, {
          open: trade.price,
          high: trade.price,
          low: trade.price,
          close: trade.price,
          volume: trade.size,
          count: 1,
        });
      }
    }
    // Yield to main thread between chunks
    if (i + chunkSize < sorted.length) {
      await new Promise(resolve => setTimeout(resolve, 0));
    }
  }

  // Fill gaps with null candles (zero volume)
  const result: OHLCV[] = [];
  let currentTime = effectiveStart - (effectiveStart % intervalMs);
  const endBucket = Math.floor(effectiveEnd / intervalMs) * intervalMs;

  while (currentTime <= endBucket) {
    const bucket = buckets.get(currentTime);
    if (bucket) {
      result.push({
        time: currentTime / 1000,
        open: bucket.open,
        high: bucket.high,
        low: bucket.low,
        close: bucket.close,
        volume: bucket.volume,
      });
    } else {
      const lastCandle = result[result.length - 1];
      if (lastCandle) {
        result.push({
          time: currentTime / 1000,
          open: lastCandle.close,
          high: lastCandle.close,
          low: lastCandle.close,
          close: lastCandle.close,
          volume: 0,
        });
      }
    }
    currentTime += intervalMs;
  }

  return result;
}

function _aggregateTradesToOHLCVSync(
  trades: TradeTick[],
  interval: AggregationInterval,
  startTime?: number,
  endTime?: number
): OHLCV[] {
  if (trades.length === 0) return [];

  const intervalMs = INTERVAL_MS[interval];
  const sorted = [...trades].sort((a, b) => a.time - b.time);

  const effectiveStart = startTime ?? sorted[0].time;
  const effectiveEnd = endTime ?? sorted[sorted.length - 1].time;

  const buckets = new Map<number, { open: number; high: number; low: number; close: number; volume: number; count: number }>();

  for (const trade of sorted) {
    if (trade.time < effectiveStart || trade.time > effectiveEnd) continue;
    const bucketTime = Math.floor(trade.time / intervalMs) * intervalMs;

    const existing = buckets.get(bucketTime);
    if (existing) {
      existing.high = Math.max(existing.high, trade.price);
      existing.low = Math.min(existing.low, trade.price);
      existing.close = trade.price;
      existing.volume += trade.size;
      existing.count++;
    } else {
      buckets.set(bucketTime, {
        open: trade.price,
        high: trade.price,
        low: trade.price,
        close: trade.price,
        volume: trade.size,
        count: 1,
      });
    }
  }

  // Fill gaps with null candles (zero volume)
  const result: OHLCV[] = [];
  let currentTime = effectiveStart - (effectiveStart % intervalMs);
  const endBucket = Math.floor(effectiveEnd / intervalMs) * intervalMs;

  while (currentTime <= endBucket) {
    const bucket = buckets.get(currentTime);
    if (bucket) {
      result.push({
        time: currentTime / 1000,
        open: bucket.open,
        high: bucket.high,
        low: bucket.low,
        close: bucket.close,
        volume: bucket.volume,
      });
    } else {
      const lastCandle = result[result.length - 1];
      if (lastCandle) {
        result.push({
          time: currentTime / 1000,
          open: lastCandle.close,
          high: lastCandle.close,
          low: lastCandle.close,
          close: lastCandle.close,
          volume: 0,
        });
      }
    }
    currentTime += intervalMs;
  }

  return result;
}

export function aggregateVolumeBySide(
  trades: TradeTick[],
  interval: AggregationInterval
): AggregatedVolume[] {
  if (trades.length === 0) return [];

  const intervalMs = INTERVAL_MS[interval];
  const sorted = [...trades].sort((a, b) => a.time - b.time);
  const buckets = new Map<number, AggregatedVolume>();

  for (const trade of sorted) {
    const bucketTime = Math.floor(trade.time / intervalMs) * intervalMs;
    const existing = buckets.get(bucketTime);

    if (existing) {
      if (trade.side === 'buy') {
        existing.buyVolume += trade.size;
        existing.buyCount++;
      } else {
        existing.sellVolume += trade.size;
        existing.sellCount++;
      }
      existing.totalVolume += trade.size;
      existing.totalCount++;
      existing.avgPrice = (existing.avgPrice * (existing.totalCount - 1) + trade.price) / existing.totalCount;
      existing.vwap = ((existing.vwap * existing.totalCount) + (trade.price * trade.size)) / (existing.totalCount + 1);
    } else {
      buckets.set(bucketTime, {
        time: bucketTime,
        buyVolume: trade.side === 'buy' ? trade.size : 0,
        sellVolume: trade.side === 'sell' ? trade.size : 0,
        totalVolume: trade.size,
        buyCount: trade.side === 'buy' ? 1 : 0,
        sellCount: trade.side === 'sell' ? 1 : 0,
        totalCount: 1,
        avgPrice: trade.price,
        vwap: trade.price,
      });
    }
  }

  return Array.from(buckets.values())
    .sort((a, b) => a.time - b.time);
}

// ---------------------------------------------------------------------------
// NORMALIZATION
// ---------------------------------------------------------------------------

export function normalizePrice(
  price: number,
  tickSize: number,
  mode: 'round' | 'floor' | 'ceil' = 'round'
): number {
  if (tickSize <= 0) return price;
  const factor = 1 / tickSize;
  switch (mode) {
    case 'floor': return Math.floor(price * factor) / factor;
    case 'ceil': return Math.ceil(price * factor) / factor;
    case 'round': return Math.round(price * factor) / factor;
  }
}

export function normalizeQuantity(
  quantity: number,
  lotSize: number,
  mode: 'round' | 'floor' | 'ceil' = 'round'
): number {
  if (lotSize <= 0) return quantity;
  const factor = 1 / lotSize;
  switch (mode) {
    case 'floor': return Math.floor(quantity * factor) * lotSize;
    case 'ceil': return Math.ceil(quantity * factor) * lotSize;
    case 'round': return Math.round(quantity * factor) * lotSize;
  }
}

export function normalizeTimestamp(
  timestamp: number,
  precision: 'seconds' | 'milliseconds' | 'microseconds' = 'milliseconds'
): number {
  switch (precision) {
    case 'seconds':
      return Math.floor(timestamp / 1000) * 1000;
    case 'milliseconds':
      return timestamp;
    case 'microseconds':
      return Math.floor(timestamp / 1000) * 1000;
  }
}

export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

export function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * clamp(t, 0, 1);
}

// ---------------------------------------------------------------------------
// STATISTICAL FUNCTIONS
// ---------------------------------------------------------------------------

export function calculateMean(values: number[]): number {
  if (values.length === 0) return 0;
  return values.reduce((sum, v) => sum + v, 0) / values.length;
}

export function calculateMedian(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[mid - 1] + sorted[mid]) / 2
    : sorted[mid];
}

export function calculateStdDev(values: number[], mean?: number): number {
  if (values.length < 2) return 0;
  const m = mean ?? calculateMean(values);
  const squaredDiffs = values.map(v => (v - m) ** 2);
  return Math.sqrt(squaredDiffs.reduce((sum, v) => sum + v, 0) / (values.length - 1));
}

export function calculateVariance(values: number[], mean?: number): number {
  const stddev = calculateStdDev(values, mean);
  return stddev * stddev;
}

export function calculatePercentile(values: number[], percentile: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.ceil(percentile / 100 * sorted.length) - 1;
  return sorted[Math.max(0, Math.min(index, sorted.length - 1))];
}

export function calculateCorrelation(x: number[], y: number[]): number {
  if (x.length !== y.length || x.length < 2) return 0;
  const meanX = calculateMean(x);
  const meanY = calculateMean(y);
  const n = x.length;

  let cov = 0;
  let varX = 0;
  let varY = 0;

  for (let i = 0; i < n; i++) {
    const dx = x[i] - meanX;
    const dy = y[i] - meanY;
    cov += dx * dy;
    varX += dx * dx;
    varY += dy * dy;
  }

  const denominator = Math.sqrt(varX * varY);
  return denominator === 0 ? 0 : cov / denominator;
}

export function calculateBeta(returns: number[], benchmarkReturns: number[]): number {
  const cov = calculateCorrelation(returns, benchmarkReturns);
  const benchVar = calculateVariance(benchmarkReturns);
  const beta = cov / benchVar;
  return isFinite(beta) ? beta : 1;
}

export function calculateAlpha(
  returns: number[],
  benchmarkReturns: number[],
  riskFreeRate: number = 0
): number {
  const avgReturn = calculateMean(returns);
  const avgBenchmark = calculateMean(benchmarkReturns);
  const beta = calculateBeta(returns, benchmarkReturns);
  return avgReturn - riskFreeRate - beta * (avgBenchmark - riskFreeRate);
}

export function calculateSharpeRatio(
  returns: number[],
  riskFreeRate: number = 0
): number {
  if (returns.length < 2) return 0;
  const excessReturns = returns.map(r => r - riskFreeRate);
  const meanExcess = calculateMean(excessReturns);
  const stdExcess = calculateStdDev(excessReturns);
  return stdExcess === 0 ? 0 : meanExcess / stdExcess;
}

export function calculateMaxDrawdown(values: number[]): { maxDrawdown: number; peakIndex: number; troughIndex: number } {
  if (values.length < 2) return { maxDrawdown: 0, peakIndex: 0, troughIndex: 0 };

  let peak = values[0];
  let peakIndex = 0;
  let maxDrawdown = 0;
  let troughIndex = 0;
  let currentTroughIndex = 0;

  for (let i = 1; i < values.length; i++) {
    if (values[i] > peak) {
      peak = values[i];
      peakIndex = i;
    }
    const drawdown = (values[i] - peak) / peak;
    if (drawdown < maxDrawdown) {
      maxDrawdown = drawdown;
      troughIndex = i;
      currentTroughIndex = i;
    }
  }

  return { maxDrawdown: Math.abs(maxDrawdown), peakIndex, troughIndex };
}

// ---------------------------------------------------------------------------
// TECHNICAL INDICATORS
// ---------------------------------------------------------------------------

export function calculateSMA(values: number[], period: number): (number | null)[] {
  const result: (number | null)[] = [];
  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) {
      result.push(null);
    } else {
      let sum = 0;
      for (let j = 0; j < period; j++) {
        sum += values[i - j];
      }
      result.push(sum / period);
    }
  }
  return result;
}

export function calculateEMA(values: number[], period: number): (number | null)[] {
  const result: (number | null)[] = [];
  const multiplier = 2 / (period + 1);

  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) {
      result.push(null);
    } else if (i === period - 1) {
      let sum = 0;
      for (let j = 0; j < period; j++) {
        sum += values[i - j];
      }
      result.push(sum / period);
    } else {
      const prevEma = result[i - 1]!;
      result.push((values[i] - prevEma) * multiplier + prevEma);
    }
  }
  return result;
}

export function calculateRSI(values: number[], period: number = 14): (number | null)[] {
  const result: (number | null)[] = [];
  if (values.length < period + 1) {
    return values.map(() => null);
  }

  const gains: number[] = [];
  const losses: number[] = [];

  for (let i = 1; i < values.length; i++) {
    const diff = values[i] - values[i - 1];
    gains.push(Math.max(0, diff));
    losses.push(Math.max(0, -diff));
  }

  let avgGain = gains.slice(0, period).reduce((s, v) => s + v, 0) / period;
  let avgLoss = losses.slice(0, period).reduce((s, v) => s + v, 0) / period;

  result.push(null); // No RSI for first value

  for (let i = 1; i < values.length; i++) {
    if (i < period + 1) {
      result.push(null);
    } else {
      if (i > period) {
        avgGain = (avgGain * (period - 1) + gains[i - 1]) / period;
        avgLoss = (avgLoss * (period - 1) + losses[i - 1]) / period;
      }
      if (avgLoss === 0) {
        result.push(100);
      } else {
        const rs = avgGain / avgLoss;
        result.push(100 - (100 / (1 + rs)));
      }
    }
  }

  return result;
}

export interface BollingerBands {
  upper: (number | null)[];
  middle: (number | null)[];
  lower: (number | null)[];
}

export function calculateBollingerBands(
  values: number[],
  period: number = 20,
  stdDev: number = 2
): BollingerBands {
  const middle = calculateSMA(values, period);
  const upper: (number | null)[] = [];
  const lower: (number | null)[] = [];

  for (let i = 0; i < values.length; i++) {
    if (middle[i] === null || i < period - 1) {
      upper.push(null);
      lower.push(null);
    } else {
      const slice = values.slice(i - period + 1, i + 1);
      const sd = calculateStdDev(slice, middle[i]!);
      upper.push(middle[i]! + sd * stdDev);
      lower.push(middle[i]! - sd * stdDev);
    }
  }

  return { upper, middle, lower };
}

export interface MACDResult {
  macd: (number | null)[];
  signal: (number | null)[];
  histogram: (number | null)[];
}

export function calculateMACD(
  values: number[],
  fastPeriod: number = 12,
  slowPeriod: number = 26,
  signalPeriod: number = 9
): MACDResult {
  const fastEma = calculateEMA(values, fastPeriod);
  const slowEma = calculateEMA(values, slowPeriod);

  const macdLine: (number | null)[] = [];
  for (let i = 0; i < values.length; i++) {
    if (fastEma[i] === null || slowEma[i] === null) {
      macdLine.push(null);
    } else {
      macdLine.push(fastEma[i]! - slowEma[i]!);
    }
  }

  const signalLine = calculateEMA(
    macdLine.filter((v): v is number => v !== null),
    signalPeriod
  );

  const histogram: (number | null)[] = [];
  let signalIdx = 0;
  for (let i = 0; i < values.length; i++) {
    if (macdLine[i] === null) {
      histogram.push(null);
    } else {
      while (signalIdx < signalLine.length && signalLine[signalIdx] === null) {
        signalIdx++;
      }
      if (signalIdx >= signalLine.length) {
        histogram.push(null);
      } else {
        histogram.push(macdLine[i]! - signalLine[signalIdx]!);
        signalIdx++;
      }
    }
  }

  return { macd: macdLine, signal: signalLine, histogram };
}

export function calculateVWAP(
  prices: number[],
  volumes: number[],
  period?: number
): number[] {
  const result: number[] = [];
  let cumulativePV = 0;
  let cumulativeV = 0;
  const startIdx = period ? prices.length - period : 0;

  for (let i = 0; i < prices.length; i++) {
    if (i < startIdx) {
      cumulativePV += prices[i] * volumes[i];
      cumulativeV += volumes[i];
    }
    if (i >= startIdx) {
      cumulativePV += prices[i] * volumes[i];
      cumulativeV += volumes[i];
      result.push(cumulativePV / cumulativeV);
    }
  }

  return result;
}

// ---------------------------------------------------------------------------
// FORMAT CONVERSION
// ---------------------------------------------------------------------------

export function ohlcvToLineData(ohlcv: OHLCV[], type: 'close' | 'open' | 'high' | 'low' = 'close'): { time: number; value: number }[] {
  return ohlcv.map(c => ({ time: c.time, value: c[type] }));
}

export function ohlcvToVolumeData(ohlcv: OHLCV[]): { time: number; value: number; color: string }[] {
  return ohlcv.map(c => ({
    time: c.time,
    value: c.volume,
    color: c.close >= c.open ? 'rgba(34, 197, 94, 0.3)' : 'rgba(239, 68, 68, 0.3)',
  }));
}

export function tradesToLineData(trades: TradeTick[], interval: AggregationInterval): { time: number; value: number }[] {
  const ohlcv = aggregateTradesToOHLCV(trades, interval);
  return ohlcvToLineData(ohlcv);
}

export function parseCSVToOHLCV(csvData: string): OHLCV[] {
  const lines = csvData.trim().split('\n');
  if (lines.length < 2) return [];

  const headers = lines[0].split(',').map(h => h.trim().toLowerCase());
  const timeIdx = headers.findIndex(h => h === 'time' || h === 'timestamp' || h === 'date');
  const openIdx = headers.findIndex(h => h === 'open');
  const highIdx = headers.findIndex(h => h === 'high');
  const lowIdx = headers.findIndex(h => h === 'low');
  const closeIdx = headers.findIndex(h => h === 'close' || h === 'close');
  const volumeIdx = headers.findIndex(h => h === 'volume' || h === 'vol');

  if (timeIdx === -1 || closeIdx === -1) return [];

  const result: OHLCV[] = [];
  for (let i = 1; i < lines.length; i++) {
    const cols = lines[i].split(',');
    const time = parseFloat(cols[timeIdx]);
    if (isNaN(time)) continue;

    result.push({
      time,
      open: openIdx >= 0 ? parseFloat(cols[openIdx]) : parseFloat(cols[closeIdx]),
      high: highIdx >= 0 ? parseFloat(cols[highIdx]) : parseFloat(cols[closeIdx]),
      low: lowIdx >= 0 ? parseFloat(cols[lowIdx]) : parseFloat(cols[closeIdx]),
      close: parseFloat(cols[closeIdx]),
      volume: volumeIdx >= 0 ? parseFloat(cols[volumeIdx]) : 0,
    });
  }

  return result.sort((a, b) => a.time - b.time);
}

export function serializeOHLCVToJSON(data: OHLCV[]): string {
  return JSON.stringify(data);
}

export function deserializeOHLCVFromJSON(json: string): OHLCV[] {
  try {
    return JSON.parse(json);
  } catch {
    return [];
  }
}
