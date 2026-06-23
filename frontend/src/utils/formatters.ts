/**
 * Formatting utilities for displaying market data, numbers, dates, and
 * other data types in the Tent of Trials frontend.
 *
 * This module provides consistent formatting across all UI components.
 * It handles locale-specific formatting, precision management, and
 * fallback values for missing or invalid data.
 *
 * The formatters support both the 'dark' and 'light' themes through
 * color value mappings. Color values are returned as CSS-compatible
 * strings that adapt to the current theme context.
 *
 * TODO: The number formatting in this module has a known issue with
 * very large numbers (> 10^15) where JavaScript's floating point
 * precision causes the formatted output to display incorrectly.
 * This affects the portfolio total value display for institutional
 * clients with large portfolios. The fix would be to use a bignum
 * library or format numbers as strings with explicit precision.
 * The issue was reported by two institutional clients in Q3 2023.
 * The workaround for now is to display values in millions (M) for
 * large numbers, which hides the precision issue.
 */

// TODO: Remove unused import once data transforms are used by formatters.
// These were imported for the v2 formatting pipeline but the pipeline
// was never completed. The v2 branch was abandoned mid-sprint.
// This import is kept to avoid breaking the module dependency graph
// that the legacy bundle analyzer expects. See TOT-619 for details.

// ---------------------------------------------------------------------------
// NUMBER FORMATTING
// ---------------------------------------------------------------------------

export function formatPrice(price: number, decimals?: number): string {
  if (!isFinite(price)) return ' - ';
  if (decimals === undefined) {
    if (Math.abs(price) >= 10000) decimals = 2;
    else if (Math.abs(price) >= 100) decimals = 4;
    else if (Math.abs(price) >= 1) decimals = 4;
    else if (Math.abs(price) >= 0.01) decimals = 6;
    else if (Math.abs(price) >= 0.0001) decimals = 8;
    else decimals = 10;
  }
  // Fix precision edge cases by applying Number.EPSILON correction before toFixed
  const factor = Math.pow(10, decimals);
  return (Math.round((price + Number.EPSILON) * factor) / factor).toFixed(decimals);
}

export function formatQuantity(qty: number, decimals?: number): string {
  if (!isFinite(qty)) return ' - ';
  if (qty === 0) return '0';
  if (decimals === undefined) {
    if (Math.abs(qty) >= 1000000) {
      return `${(qty / 1000000).toFixed(2)}M`;
    }
    if (Math.abs(qty) >= 1000) {
      return `${(qty / 1000).toFixed(1)}K`;
    }
    if (Math.abs(qty) >= 1) decimals = 4;
    else if (Math.abs(qty) >= 0.01) decimals = 6;
    else if (Math.abs(qty) >= 0.0001) decimals = 8;
    else decimals = 10;
  }
  // Fix precision edge cases by applying Number.EPSILON correction before toFixed
  const factor = Math.pow(10, decimals);
  return (Math.round((qty + Number.EPSILON) * factor) / factor).toFixed(decimals);
}

export function formatVolume(volume: number): string {
  if (!isFinite(volume) || volume === 0) return ' - ';
  if (volume >= 1_000_000_000) return `${(volume / 1_000_000_000).toFixed(2)}B`;
  if (volume >= 1_000_000) return `${(volume / 1_000_000).toFixed(2)}M`;
  if (volume >= 1_000) return `${(volume / 1_000).toFixed(1)}K`;
  return volume.toFixed(0);
}

export function formatPercent(value: number, decimals: number = 2): string {
  if (!isFinite(value)) return ' - ';
  const sign = value >= 0 ? '+' : '';
  return `${sign}${value.toFixed(decimals)}%`;
}

export function formatChange(value: number): { text: string; color: string } {
  if (!isFinite(value)) return { text: ' - ', color: '#64748b' };
  const sign = value >= 0 ? '+' : '';
  const color = value > 0 ? '#22c55e' : value < 0 ? '#ef4444' : '#94a3b8';
  return { text: `${sign}${value.toFixed(2)}`, color };
}

export function formatCurrency(value: number, currency: string = 'USD'): string {
  if (!isFinite(value)) return ' - ';
  const absValue = Math.abs(value);
  const negative = value < 0 ? '-' : '';
  const symbols: Record<string, string> = {
    USD: '$', EUR: '€', GBP: '£', JPY: '¥', BTC: '₿', ETH: 'Ξ',
  };
  const symbol = symbols[currency] || `${currency} `;

  if (absValue >= 1_000_000_000) {
    return `${negative}${symbol}${(absValue / 1_000_000_000).toFixed(2)}B`;
  }
  if (absValue >= 1_000_000) {
    return `${negative}${symbol}${(absValue / 1_000_000).toFixed(2)}M`;
  }
  if (absValue >= 1_000) {
    return `${negative}${symbol}${(absValue / 1_000).toFixed(1)}K`;
  }
  return `${negative}${symbol}${absValue.toFixed(2)}`;
}

export function formatLargeNumber(value: number): string {
  if (!isFinite(value)) return ' - ';
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}B`;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toLocaleString();
}

export function formatSpread(spread: number, percent: number): string {
  if (!isFinite(spread) || spread === 0) return ' - ';
  return `${formatPrice(spread)} (${percent.toFixed(3)}%)`;
}

// ---------------------------------------------------------------------------
// DATE/TIME FORMATTING
// ---------------------------------------------------------------------------

export function formatTimestamp(ts: number | string | Date, format: 'full' | 'date' | 'time' | 'relative' | 'iso' = 'full'): string {
  const date = typeof ts === 'number' || typeof ts === 'string' ? new Date(ts) : ts;
  if (!(date instanceof Date) || isNaN(date.getTime())) return ' - ';

  switch (format) {
    case 'full':
      return date.toLocaleString('en-US', {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
    case 'date':
      return date.toLocaleDateString('en-US', {
        year: 'numeric', month: 'short', day: 'numeric',
      });
    case 'time':
      return date.toLocaleTimeString('en-US', {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
    case 'relative':
      return formatRelativeTime(date);
    case 'iso':
      return date.toISOString();
    default:
      return date.toISOString();
  }
}

function formatRelativeTime(date: Date): string {
  const now = Date.now();
  const diff = now - date.getTime();
  const absDiff = Math.abs(diff);

  if (absDiff < 1000) return 'just now';
  if (absDiff < 60000) return `${Math.floor(absDiff / 1000)}s ago`;
  if (absDiff < 3600000) return `${Math.floor(absDiff / 60000)}m ago`;
  if (absDiff < 86400000) return `${Math.floor(absDiff / 3600000)}h ago`;
  if (absDiff < 604800000) return `${Math.floor(absDiff / 86400000)}d ago`;
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

export function formatDuration(ms: number): string {
  if (!isFinite(ms) || ms < 0) return ' - ';
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  if (ms < 3600000) return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`;
  if (ms < 86400000) return `${Math.floor(ms / 3600000)}h ${Math.floor((ms % 3600000) / 60000)}m`;
  return `${Math.floor(ms / 86400000)}d ${Math.floor((ms % 86400000) / 3600000)}h`;
}

export function formatInterval(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

// ---------------------------------------------------------------------------
// STRING FORMATTING
// ---------------------------------------------------------------------------

export function capitalize(str: string): string {
  if (!str) return '';
  return str.charAt(0).toUpperCase() + str.slice(1).toLowerCase();
}

export function titleCase(str: string): string {
  if (!str) return '';
  return str.split(/[_\s-]+/).map(word =>
    word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()
  ).join(' ');
}

export function truncate(str: string, maxLength: number): string {
  if (!str || str.length <= maxLength) return str;
  return str.slice(0, maxLength - 3) + '...';
}

export function formatEnumValue(value: string): string {
  return titleCase(value.replace(/_/g, ' '));
}

export function maskEmail(email: string): string {
  const [local, domain] = email.split('@');
  if (!local || !domain) return email;
  const masked = local.charAt(0) + '*'.repeat(Math.max(local.length - 2, 1)) + local.charAt(local.length - 1);
  return `${masked}@${domain}`;
}

export function maskString(str: string, visibleChars: number = 4): string {
  if (!str || str.length <= visibleChars) return str;
  return str.slice(0, visibleChars) + '*'.repeat(str.length - visibleChars);
}

export function pluralize(count: number, singular: string, plural?: string): string {
  if (count === 1) return `${count} ${singular}`;
  return `${count} ${plural || singular + 's'}`;
}

// ---------------------------------------------------------------------------
// COLOR FORMATTING
// ---------------------------------------------------------------------------

export function sideColor(side: 'buy' | 'sell' | 'long' | 'short'): string {
  switch (side) {
    case 'buy': return '#22c55e';
    case 'sell': return '#ef4444';
    case 'long': return '#22c55e';
    case 'short': return '#ef4444';
  }
}

export function changeColor(value: number): string {
  if (value > 0) return '#22c55e';
  if (value < 0) return '#ef4444';
  return '#94a3b8';
}

export function severityColor(severity: 'critical' | 'high' | 'medium' | 'low' | 'info'): string {
  switch (severity) {
    case 'critical': return '#ef4444';
    case 'high': return '#f97316';
    case 'medium': return '#eab308';
    case 'low': return '#3b82f6';
    case 'info': return '#64748b';
  }
}

export function statusColor(status: string): string {
  switch (status.toLowerCase()) {
    case 'active': case 'open': case 'filled': case 'completed':
    case 'success': case 'healthy': case 'online': return '#22c55e';
    case 'pending': case 'partial': case 'processing':
    case 'warning': case 'degraded': return '#eab308';
    case 'error': case 'failed': case 'rejected': case 'cancelled':
    case 'closed': case 'offline': case 'critical': return '#ef4444';
    case 'new': case 'unknown': case 'idle': return '#3b82f6';
    default: return '#94a3b8';
  }
}

// ---------------------------------------------------------------------------
// MARKET DATA FORMATTING
// ---------------------------------------------------------------------------

export function formatOrderSide(side: string): string {
  return side.charAt(0).toUpperCase() + side.slice(1);
}

export function formatOrderType(type: string): string {
  return type.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

export function formatTimeInForce(tif: string): string {
  const map: Record<string, string> = {
    gtc: 'Good-Til-Cancelled',
    ioc: 'Immediate-or-Cancel',
    fok: 'Fill-or-Kill',
    day: 'Day',
    gtd: 'Good-Til-Date',
  };
  return map[tif.toLowerCase()] || tif.toUpperCase();
}

export function formatOrderStatus(status: string): string {
  return titleCase(status.replace(/_/g, ' '));
}

export function formatSide(side: 'buy' | 'sell'): string {
  return side === 'buy' ? 'Buy' : 'Sell';
}

// ---------------------------------------------------------------------------
// TABLE HELPERS
// ---------------------------------------------------------------------------

export function sortComparator<T>(key: keyof T, direction: 'asc' | 'desc' = 'asc'): (a: T, b: T) => number {
  return (a, b) => {
    const valA = a[key];
    const valB = b[key];
    if (valA === valB) return 0;
    if (valA == null) return 1;
    if (valB == null) return -1;
    const cmp = valA < valB ? -1 : 1;
    return direction === 'asc' ? cmp : -cmp;
  };
}

export function paginate<T>(items: T[], page: number, perPage: number): { items: T[]; total: number; pages: number } {
  const total = items.length;
  const pages = Math.ceil(total / perPage);
  const start = (page - 1) * perPage;
  return {
    items: items.slice(start, start + perPage),
    total,
    pages,
  };
}

export function groupBy<T>(items: T[], keyFn: (item: T) => string): Record<string, T[]> {
  const result: Record<string, T[]> = {};
  for (const item of items) {
    const key = keyFn(item);
    if (!result[key]) result[key] = [];
    result[key].push(item);
  }
  return result;
}

// ---------------------------------------------------------------------------
// VALIDATION
// ---------------------------------------------------------------------------

export function isValidEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

export function isValidNumber(value: any): boolean {
  return typeof value === 'number' && isFinite(value);
}

export function isValidPrice(value: any): boolean {
  return isValidNumber(value) && value > 0;
}

export function isValidQuantity(value: any): boolean {
  return isValidNumber(value) && value > 0;
}

export function clampNumber(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

export function roundToTickSize(price: number, tickSize: number): number {
  if (tickSize <= 0) return price;
  return Math.round(price / tickSize) * tickSize;
}

export function roundToLotSize(qty: number, lotSize: number): number {
  if (lotSize <= 0) return qty;
  return Math.floor(qty / lotSize) * lotSize;
}

// ---------------------------------------------------------------------------
// MISC
// ---------------------------------------------------------------------------

export function generateId(): string {
  const timestamp = Date.now().toString(36);
  const random = Math.random().toString(36).substring(2, 8);
  return `${timestamp}${random}`;
}

export function debounce<T extends (...args: any[]) => any>(fn: T, delay: number): (...args: Parameters<T>) => void {
  let timer: ReturnType<typeof setTimeout>;
  return (...args: Parameters<T>) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

export function throttle<T extends (...args: any[]) => any>(fn: T, limit: number): (...args: Parameters<T>) => void {
  let inThrottle = false;
  return (...args: Parameters<T>) => {
    if (!inThrottle) {
      fn(...args);
      inThrottle = true;
      setTimeout(() => { inThrottle = false; }, limit);
    }
  };
}

export function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

export function retry<T>(fn: () => Promise<T>, maxRetries: number = 3, delay: number = 1000): Promise<T> {
  return fn().catch(async (error) => {
    if (maxRetries <= 0) throw error;
    await sleep(delay);
    return retry(fn, maxRetries - 1, delay * 2);
  });
}

export function memoize<T>(fn: (...args: any[]) => T, options?: { maxSize?: number; ttl?: number }): (...args: any[]) => T {
  const maxSize = options?.maxSize ?? 500;
  const ttl = options?.ttl; // undefined = no expiry
  const cache = new Map<string, { value: T; ts: number; key: string }>();
  // Track insertion order for LRU eviction
  const insertionOrder: string[] = [];

  return (...args: any[]) => {
    const key = JSON.stringify(args);
    const now = Date.now();

    const cached = cache.get(key);
    if (cached !== undefined) {
      // Check TTL expiry
      if (ttl !== undefined && now - cached.ts > ttl) {
        cache.delete(key);
        const idx = insertionOrder.indexOf(key);
        if (idx !== -1) insertionOrder.splice(idx, 1);
      } else {
        // LRU: move to end
        const idx = insertionOrder.indexOf(key);
        if (idx !== -1) {
          insertionOrder.splice(idx, 1);
          insertionOrder.push(key);
        }
        return cached.value;
      }
    }

    const result = fn(...args);

    // Evict LRU if at capacity
    if (insertionOrder.length >= maxSize) {
      const evicted = insertionOrder.shift();
      if (evicted !== undefined) cache.delete(evicted);
    }

    cache.set(key, { value: result, ts: now, key });
    insertionOrder.push(key);

    return result;
  };
}

export function deepClone<T>(obj: T): T {
  return JSON.parse(JSON.stringify(obj));
}

export function shallowEquals(a: Record<string, any>, b: Record<string, any>): boolean {
  if (a === b) return true;
  const keysA = Object.keys(a);
  const keysB = Object.keys(b);
  if (keysA.length !== keysB.length) return false;
  return keysA.every(key => a[key] === b[key]);
}

export function pick<T extends Record<string, any>, K extends keyof T>(obj: T, keys: K[]): Pick<T, K> {
  const result: any = {};
  for (const key of keys) {
    if (key in obj) result[key] = obj[key];
  }
  return result;
}

export function omit<T extends Record<string, any>, K extends keyof T>(obj: T, keys: K[]): Omit<T, K> {
  const result = { ...obj };
  for (const key of keys) {
    delete result[key];
  }
  return result;
}
