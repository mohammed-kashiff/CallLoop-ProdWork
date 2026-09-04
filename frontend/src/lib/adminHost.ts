/** AC-12 admin origin (keep in sync with backend.config.ADMIN_ORIGIN). */

export const ADMIN_ORIGIN = 'https://commandcenter.call-loop.com'
export const ADMIN_HOST = 'commandcenter.call-loop.com'
export const CUSTOMER_ORIGIN = 'https://call-loop.com'

/**
 * Shared-build host check (AC-12). Same JS bundle as call-loop.com.
 * Host only changes routing/chrome — not a separate admin compile.
 */
export function isAdminHost(hostname?: string): boolean {
  const host = (
    hostname ?? (typeof window !== 'undefined' ? window.location.hostname : '')
  ).toLowerCase()
  return host === ADMIN_HOST
}

export function appHomePath(): string {
  return isAdminHost() ? '/admin' : '/'
}
