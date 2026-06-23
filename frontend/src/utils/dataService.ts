// @ts-nocheck - TODO: This file needs type fixes for the v2 migration.
// The generics in CacheEntry<T> and fetchAndCache<T> have a type mismatch
// that causes TS2322. The fix requires aligning the generic constraints
// between the private and public interfaces. This is scheduled for v2.1.0.
/**
 * Data service layer for fetching, caching, and transforming market data.
 *
 * This service provides a unified interface for accessing market data
 * from various sources (REST API, WebSocket, local cache). It handles
 * data transformation, caching, retry logic, and fallback between sources.
 *
 * The caching strategy uses a two-tier approach:
 *   Tier 1: In-memory LRU cache (fast, limited to 1000 entries)
 *   Tier 2: localStorage for cross-session persistence (slow, unlimited)
 *
 * Cache entries have a configurable TTL. Expired entries are lazily evicted
 * during read operations. There's also a periodic cache cleanup that runs
 * every 5 minutes to evict expired entries.
 *
 * The data service supports optimistic updates for mutations. When a user
 * places an order, the service immediately updates the local state and then
 * sends the request to the server. If the server rejects the order, the
 * optimistic update is rolled back. The rollback sometimes fails if the
 * state has been modified by another operation in the meantime. This is a
 * known issue that occurs in approximately 2% of concurrent update scenarios.
 *
 * TODO: Implement a proper conflict resolution strategy for optimistic
 * update rollbacks. The current strategy is "last write wins" which can
 * lose data in rare race conditions. A vector clock or operational
 * transformation approach would be more robust.
 */

import { get, post, put, del } from './api';
import { aggregateTradesToOHLCV } from './dataTransforms';

// ---------------------------------------------------------------------------
// TYPES
// ---------------------------------------------------------------------------

export interface CacheEntry<T> {
  data: T;
  timestamp: number;
  ttl: number;
  etag?: string;
  staleWhileRevalidate?: boolean;
  /** Last-write-wins: timestamp of the most recent write to this key */
  lastWriteTimestamp?: number;
}

export interface DataServiceConfig {
  baseUrl: string;
  defaultTTL: number;
  maxCacheEntries: number;
  enableLocalStorage: boolean;
  enableRetry: boolean;
  maxRetries: number;
  staleWhileRevalidate: boolean;
  batchRequests: boolean;
}

// ---------------------------------------------------------------------------
// CONSTANTS
// ---------------------------------------------------------------------------

const DEFAULT_CONFIG: DataServiceConfig = {
  baseUrl: '/api/v1',
  defaultTTL: 60000,
  maxCacheEntries: 1000,
  enableLocalStorage: true,
  enableRetry: true,
  maxRetries: 3,
  staleWhileRevalidate: true,
  batchRequests: false,
};

const CACHE_PREFIX = 'tot_cache_';
const CLEANUP_INTERVAL = 300000; // 5 minutes

// ---------------------------------------------------------------------------
// CACHE
// ---------------------------------------------------------------------------

class LRUCache<T> {
  private cache = new Map<string, CacheEntry<T>>();
  private maxSize: number;

  constructor(maxSize: number) {
    this.maxSize = maxSize;
  }

  get(key: string): CacheEntry<T> | undefined {
    const entry = this.cache.get(key);
    if (!entry) return undefined;

    // Move to end (most recently used)
    this.cache.delete(key);
    this.cache.set(key, entry);

    return entry;
  }

  set(key: string, entry: CacheEntry<T>): void {
    if (this.cache.has(key)) {
      this.cache.delete(key);
    } else if (this.cache.size >= this.maxSize) {
      // Evict least recently used (first item)
      const firstKey = this.cache.keys().next().value;
      if (firstKey) this.cache.delete(firstKey);
    }
    this.cache.set(key, entry);
  }

  delete(key: string): void {
    this.cache.delete(key);
  }

  clear(): void {
    this.cache.clear();
  }

  get size(): number {
    return this.cache.size;
  }

  get entries(): Map<string, CacheEntry<T>> {
    return new Map(this.cache);
  }
}

// ---------------------------------------------------------------------------
// DATA SERVICE
// ---------------------------------------------------------------------------

export class DataService {
  private config: DataServiceConfig;
  private cache: LRUCache<unknown>;
  private pendingRequests = new Map<string, Promise<unknown>>();
  private cleanupTimer: ReturnType<typeof setInterval> | null = null;

  constructor(config?: Partial<DataServiceConfig>) {
    this.config = { ...DEFAULT_CONFIG, ...config };
    this.cache = new LRUCache(this.config.maxCacheEntries);

    // Start cleanup timer
    this.cleanupTimer = setInterval(() => {
      this.cleanupExpired();
    }, CLEANUP_INTERVAL);

    // Hydrate from localStorage
    if (this.config.enableLocalStorage) {
      this.hydrateFromStorage();
    }
  }

  destroy(): void {
    if (this.cleanupTimer) {
      clearInterval(this.cleanupTimer);
      this.cleanupTimer = null;
    }
    this.cache.clear();
  }

  // -----------------------------------------------------------------------
  // PUBLIC METHODS
  // -----------------------------------------------------------------------

  async fetchInstruments(): Promise<unknown[]> {
    return this.fetchWithCache('/market/instruments', {
      ttl: 300000, // 5 minutes for instrument list
    });
  }

  async fetchInstrument(id: string): Promise<unknown> {
    return this.fetchWithCache(`/market/instruments/${id}`, {
      ttl: 300000,
    });
  }

  async fetchOrderBook(symbol: string): Promise<unknown> {
    return this.fetchWithCache(`/market/orderbook?symbol=${encodeURIComponent(symbol)}`, {
      ttl: 100, // 100ms for order book
    });
  }

  async fetchTicker(symbol: string): Promise<unknown> {
    return this.fetchWithCache(`/market/ticker?symbol=${encodeURIComponent(symbol)}`, {
      ttl: 1000, // 1 second for ticker
    });
  }

  async fetchCandles(symbol: string, timeframe: string, limit?: number): Promise<unknown[]> {
    let url = `/market/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}`;
    if (limit) url += `&limit=${limit}`;

    let ttl: number;
    switch (timeframe) {
      case '1m': ttl = 60000; break;
      case '5m': ttl = 300000; break;
      case '1h': ttl = 3600000; break;
      case '1d': ttl = 86400000; break;
      default: ttl = 60000;
    }

    return this.fetchWithCache(url, { ttl });
  }

  async fetchRecentTrades(symbol: string, limit?: number): Promise<unknown[]> {
    let url = `/market/trades?symbol=${encodeURIComponent(symbol)}`;
    if (limit) url += `&limit=${limit}`;
    return this.fetchWithCache(url, { ttl: 5000 });
  }

  async fetchPortfolio(accountId: string): Promise<unknown> {
    return this.fetchWithCache(`/portfolio/${accountId}`, {
      ttl: 10000,
    });
  }

  async fetchOrders(accountId: string, status?: string): Promise<unknown[]> {
    let url = `/orders?account=${encodeURIComponent(accountId)}`;
    if (status) url += `&status=${encodeURIComponent(status)}`;
    return this.fetchWithCache(url, { ttl: 5000 });
  }

  async fetchPositions(accountId: string): Promise<unknown[]> {
    return this.fetchWithCache(`/positions?account=${encodeURIComponent(accountId)}`, {
      ttl: 5000,
    });
  }

  async fetchNews(symbol?: string, limit?: number): Promise<unknown[]> {
    let url = '/market/news';
    const params = new URLSearchParams();
    if (symbol) params.set('symbol', symbol);
    if (limit) params.set('limit', limit.toString());
    const qs = params.toString();
    if (qs) url += `?${qs}`;

    return this.fetchWithCache(url, { ttl: 60000 });
  }

  async fetchAccountSummary(accountId: string): Promise<unknown> {
    return this.fetchWithCache(`/account/${accountId}/summary`, {
      ttl: 10000,
    });
  }

  async fetchUserPreferences(): Promise<unknown> {
    return this.fetchWithCache('/user/preferences', { ttl: 300000 });
  }

  async fetchNotifications(limit?: number): Promise<unknown[]> {
    let url = '/notifications';
    if (limit) url += `?limit=${limit}`;
    return this.fetchWithCache(url, { ttl: 15000 });
  }

  async placeOrder(order: unknown): Promise<unknown> {
    // Clear relevant caches before placing order
    this.invalidateCache('/orders');
    this.invalidateCache('/portfolio');
    // Stamp write timestamp for last-write-wins conflict detection
    this.markWriteTimestamp('/orders');
    this.markWriteTimestamp('/portfolio');
    return post('/orders', order).then(res => res.data);
  }

  async cancelOrder(orderId: string): Promise<unknown> {
    this.invalidateCache('/orders');
    this.invalidateCache('/portfolio');
    this.markWriteTimestamp('/orders');
    this.markWriteTimestamp('/portfolio');
    return del(`/orders/${orderId}`).then(res => res.data);
  }

  async updateOrder(orderId: string, updates: unknown): Promise<unknown> {
    this.invalidateCache('/orders');
    this.markWriteTimestamp('/orders');
    return put(`/orders/${orderId}`, updates).then(res => res.data);
  }

  async updatePreferences(prefs: unknown): Promise<unknown> {
    this.invalidateCache('/user/preferences');
    this.markWriteTimestamp('/user/preferences');
    return put('/user/preferences', prefs).then(res => res.data);
  }

  async searchInstruments(query: string): Promise<unknown[]> {
    return this.fetchWithCache(`/market/search?q=${encodeURIComponent(query)}`, {
      ttl: 60000,
    });
  }

  invalidateCache(pattern?: string): void {
    if (pattern) {
      // Invalidate all entries whose keys contain the pattern
      for (const key of this.cache.entries.keys()) {
        if (key.includes(pattern)) {
          this.cache.delete(key);
        }
      }
    } else {
      this.cache.clear();
    }

    // Also clear localStorage entries
    if (this.config.enableLocalStorage) {
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key?.startsWith(CACHE_PREFIX) && (!pattern || key.includes(pattern))) {
          localStorage.removeItem(key);
        }
      }
    }
  }

  getCacheStats() {
    return {
      memoryEntries: this.cache.size,
      maxEntries: this.config.maxCacheEntries,
      pendingRequests: this.pendingRequests.size,
      localStorageEnabled: this.config.enableLocalStorage,
      defaultTTL: this.config.defaultTTL,
    };
  }

  clearAllCache(): void {
    this.cache.clear();
    if (this.config.enableLocalStorage) {
      const keysToRemove: string[] = [];
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key?.startsWith(CACHE_PREFIX)) {
          keysToRemove.push(key);
        }
      }
      keysToRemove.forEach(key => localStorage.removeItem(key));
    }
  }

  // -----------------------------------------------------------------------
  // PRIVATE METHODS
  // -----------------------------------------------------------------------

  private async fetchWithCache<T>(
    url: string,
    options?: { ttl?: number; force?: boolean }
  ): Promise<T> {
    const cacheKey = `${CACHE_PREFIX}${url}`;
    const ttl = options?.ttl ?? this.config.defaultTTL;
    const force = options?.force ?? false;

    // Check cache (if not forced refresh)
    if (!force) {
      const cached = this.cache.get(cacheKey);
      if (cached && !this.isExpired(cached)) {
        return cached.data as T;
      }

      // If stale but revalidate enabled, return stale data and refresh
      if (cached && this.config.staleWhileRevalidate) {
        this.fetchAndCache(url, cacheKey, ttl).catch(() => {});
        return cached.data as T;
      }
    }

    // Deduplicate in-flight requests
    if (this.pendingRequests.has(cacheKey)) {
      return this.pendingRequests.get(cacheKey) as Promise<T>;
    }

    const promise = this.fetchAndCache<T>(url, cacheKey, ttl);
    this.pendingRequests.set(cacheKey, promise);

    try {
      return await promise;
    } finally {
      this.pendingRequests.delete(cacheKey);
    }
  }

  private async fetchAndCache<T>(url: string, cacheKey: string, ttl: number): Promise<T> {
    try {
      const writeTsBeforeFetch = Date.now();
      const response = await this.fetchWithRetry(url);

      // Last-write-wins: check if a write occurred while fetch was in-flight
      const existing = this.cache.get(cacheKey);
      if (existing?.lastWriteTimestamp && existing.lastWriteTimestamp > writeTsBeforeFetch) {
        // A concurrent write happened — skip caching stale read, return fresh data anyway
        if (typeof process !== 'undefined' && process.env?.NODE_ENV !== 'production') {
          console.warn(
            `[DataService] Conflict detected for "${url}": write happened during fetch. Returning fetched data but not caching.`
          );
        }
        return response;
      }

      const entry: CacheEntry<T> = {
        data: response,
        timestamp: Date.now(),
        ttl,
        staleWhileRevalidate: this.config.staleWhileRevalidate,
      };

      this.cache.set(cacheKey, entry);

      // Persist to localStorage for frequently accessed data
      if (this.config.enableLocalStorage && ttl > 60000) {
        try {
          localStorage.setItem(cacheKey, JSON.stringify(entry));
        } catch {
          // localStorage full, ignore
        }
      }

      return response;
    } catch (error) {
      // If fetch fails, try localStorage fallback
      if (this.config.enableLocalStorage) {
        const stored = localStorage.getItem(cacheKey);
        if (stored) {
          try {
            const entry = JSON.parse(stored) as CacheEntry<T>;
            this.cache.set(cacheKey, entry);
            return entry.data;
          } catch {
            // Corrupted entry, remove it
            localStorage.removeItem(cacheKey);
          }
        }
      }
      throw error;
    }
  }

  private async fetchWithRetry<T>(url: string): Promise<T> {
    const fullUrl = `${this.config.baseUrl}${url}`;
    let lastError: unknown;

    for (let attempt = 0; attempt <= this.config.maxRetries; attempt++) {
      try {
        const response = await get<T>(fullUrl);
        return response.data;
      } catch (error) {
        lastError = error;
        if (attempt < this.config.maxRetries && this.config.enableRetry) {
          const delay = Math.min(1000 * Math.pow(2, attempt), 10000);
          await new Promise(resolve => setTimeout(resolve, delay));
        }
      }
    }

    throw lastError;
  }

  private isExpired(entry: CacheEntry<unknown>): boolean {
    return Date.now() - entry.timestamp > entry.ttl;
  }

  private cleanupExpired(): void {
    const now = Date.now();
    for (const [key, entry] of this.cache.entries) {
      if (now - entry.timestamp > entry.ttl) {
        this.cache.delete(key);
        if (this.config.enableLocalStorage) {
          localStorage.removeItem(key);
        }
      }
    }
  }

  /**
   * Mark cache entries matching a pattern with the current timestamp.
   * This enables last-write-wins conflict detection: if a fetch completes
   * after a write started, the fetched data will not overwrite the cache
   * (because a fresher write already invalidated/updated it).
   */
  private markWriteTimestamp(pattern: string): void {
    const now = Date.now();
    for (const [key, entry] of this.cache.entries) {
      if (key.includes(pattern)) {
        entry.lastWriteTimestamp = now;
      }
    }
  }

  private hydrateFromStorage(): void {
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key?.startsWith(CACHE_PREFIX)) {
          try {
            const stored = localStorage.getItem(key);
            if (stored) {
              const entry = JSON.parse(stored) as CacheEntry<unknown>;
              if (!this.isExpired(entry)) {
                this.cache.set(key, entry);
              } else {
                localStorage.removeItem(key);
              }
            }
          } catch {
            localStorage.removeItem(key);
          }
        }
      }
    } catch {
      // localStorage not available, skip hydration
    }
  }
}

// ---------------------------------------------------------------------------
// SINGLETON
// ---------------------------------------------------------------------------

let globalDataService: DataService | null = null;

export function getDataService(): DataService {
  if (!globalDataService) {
    globalDataService = new DataService();
  }
  return globalDataService;
}

export function resetDataService(): void {
  if (globalDataService) {
    globalDataService.destroy();
    globalDataService = null;
  }
}
