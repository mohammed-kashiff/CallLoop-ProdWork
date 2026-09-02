/** AC-12 admin origin (keep in sync with backend.config.IDB_ORIGIN). */

export const IDB_ORIGIN = 'https://idb.call-loop.com'
export const IDB_HOST = 'idb.call-loop.com'

/**
 * Shared-build host check (AC-12). Same JS bundle as call-loop.com.
 * Host only changes routing/chrome — not a separate admin compile.
 */
export function isAdminHost(hostname?: string): boolean {
  const host = (
    hostname ?? (typeof window !== 'undefined' ? window.location.hostname : '')
  ).toLowerCase()
  return host === IDB_HOST
}

export function appHomePath(): string {
  return isAdminHost() ? '/admin' : '/'
}
