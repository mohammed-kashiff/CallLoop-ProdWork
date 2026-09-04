import { createClient, type SupabaseClient } from '@supabase/supabase-js'

const url = String(import.meta.env.VITE_SUPABASE_URL || '').replace(/\/$/, '')
const anon = String(import.meta.env.VITE_SUPABASE_ANON_KEY || '').trim()

export const supabaseConfigured = Boolean(url && anon)

export const RECOVERY_HINT = 'callloop_pw_recovery'

export function markPasswordRecovery(): void {
  try {
    sessionStorage.setItem(RECOVERY_HINT, '1')
  } catch {
    /* private mode */
  }
}

export function clearPasswordRecovery(): void {
  try {
    sessionStorage.removeItem(RECOVERY_HINT)
  } catch {
    /* ignore */
  }
}

export function hasPasswordRecoveryHint(): boolean {
  try {
    return sessionStorage.getItem(RECOVERY_HINT) === '1'
  } catch {
    return false
  }
}

const IMPERSONATION_HINT = 'callloop_impersonating'

export type ImpersonationInfo = { orgName: string; targetEmail: string }

/** Session flag only — the access/refresh tokens themselves are Supabase's
 * own hash-fragment params and are handled entirely by detectSessionInUrl.
 * This just remembers "that session-establishing load was an admin
 * impersonation" so the banner can render after the hash is consumed. */
export function markImpersonating(info: ImpersonationInfo): void {
  try {
    sessionStorage.setItem(IMPERSONATION_HINT, JSON.stringify(info))
  } catch {
    /* private mode */
  }
}

export function clearImpersonating(): void {
  try {
    sessionStorage.removeItem(IMPERSONATION_HINT)
  } catch {
    /* ignore */
  }
}

export function getImpersonating(): ImpersonationInfo | null {
  try {
    const raw = sessionStorage.getItem(IMPERSONATION_HINT)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<ImpersonationInfo>
    if (!parsed.orgName || !parsed.targetEmail) return null
    return { orgName: parsed.orgName, targetEmail: parsed.targetEmail }
  } catch {
    return null
  }
}

/** Picks the one-time ?impersonated=1&org=..&as=.. query params off a fresh
 * "Log in as" tab (the access/refresh tokens ride in the hash instead, where
 * detectSessionInUrl already reads them) and strips them from the URL. */
function adoptImpersonationUrl(): void {
  if (typeof window === 'undefined') return
  const query = new URLSearchParams(window.location.search)
  if (query.get('impersonated') !== '1') return
  const orgName = query.get('org') || ''
  const targetEmail = query.get('as') || ''
  if (orgName && targetEmail) markImpersonating({ orgName, targetEmail })
  query.delete('impersonated')
  query.delete('org')
  query.delete('as')
  const rest = query.toString()
  window.history.replaceState(
    null,
    '',
    `${window.location.pathname}${rest ? `?${rest}` : ''}${window.location.hash}`,
  )
}

adoptImpersonationUrl()

function isResetPasswordPath(pathname: string): boolean {
  const path = pathname.replace(/\/+$/, '') || '/'
  return path === '/reset-password'
}

/** If a recovery link lands on the site root (Site URL), keep the tokens and put them on /reset-password. */
function adoptRecoveryUrl(): void {
  if (typeof window === 'undefined') return
  const hash = new URLSearchParams(window.location.hash.replace(/^#/, ''))
  const query = new URLSearchParams(window.location.search)
  const path = window.location.pathname.replace(/\/+$/, '') || '/'
  const recoveryType = hash.get('type') === 'recovery' || query.get('type') === 'recovery'
  const recoveryCodeOnHome =
    Boolean(query.get('code')) && (path === '/' || path === '/login')
  if (isResetPasswordPath(path)) {
    if (recoveryType || query.get('code') || hash.get('access_token')) {
      markPasswordRecovery()
    }
    return
  }
  if (!recoveryType && !recoveryCodeOnHome) return
  markPasswordRecovery()
  window.history.replaceState(
    null,
    '',
    `/reset-password${window.location.search}${window.location.hash}`,
  )
}

adoptRecoveryUrl()

export const supabase: SupabaseClient | null = supabaseConfigured
  ? createClient(url, anon, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
      },
    })
  : null
