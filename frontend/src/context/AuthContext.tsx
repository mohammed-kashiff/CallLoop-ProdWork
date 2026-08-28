import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import type { Session } from '@supabase/supabase-js'
import { apiFetch } from '../lib/api'
import type { FeatureMap } from '../lib/features'
import { supabase, supabaseConfigured, hasPasswordRecoveryHint, markPasswordRecovery, clearPasswordRecovery } from '../lib/supabase'

type AuthContextValue = {
  configured: boolean
  loading: boolean
  session: Session | null
  accessToken: string | null
  email: string | null
  features: FeatureMap
  isPlatformAdmin: boolean
  passwordRecovery: boolean
  refreshMe: () => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(supabaseConfigured)
  const [session, setSession] = useState<Session | null>(null)
  const [features, setFeatures] = useState<FeatureMap>({})
  const [isPlatformAdmin, setIsPlatformAdmin] = useState(false)
  const [passwordRecovery, setPasswordRecovery] = useState(hasPasswordRecoveryHint)

  const email = session?.user?.email ?? null

  const refreshMe = useCallback(async () => {
    if (!session) {
      setFeatures({})
      setIsPlatformAdmin(false)
      return
    }
    const r = await apiFetch('/api/me')
    if (!r.ok) {
      setIsPlatformAdmin(false)
      return
    }
    const data = (await r.json()) as {
      features?: FeatureMap
      is_platform_admin?: boolean
    }
    if (data.features && typeof data.features === 'object') {
      setFeatures(data.features)
    }
    setIsPlatformAdmin(data.is_platform_admin === true)
  }, [session])

  useEffect(() => {
    if (!supabase) {
      setLoading(false)
      return
    }
    let cancelled = false
    supabase.auth.getSession().then(({ data }) => {
      if (!cancelled) {
        setSession(data.session ?? null)
        setLoading(false)
      }
    })
    const { data: sub } = supabase.auth.onAuthStateChange((event, next) => {
      if (event === 'PASSWORD_RECOVERY') {
        markPasswordRecovery()
        setPasswordRecovery(true)
      }
      if (event === 'SIGNED_OUT') {
        clearPasswordRecovery()
        setPasswordRecovery(false)
      }
      setSession(next)
      setLoading(false)
    })
    return () => {
      cancelled = true
      sub.subscription.unsubscribe()
    }
  }, [])

  useEffect(() => {
    void refreshMe()
  }, [refreshMe])

  const signOut = useCallback(async () => {
    clearPasswordRecovery()
    setPasswordRecovery(false)
    if (supabase) await supabase.auth.signOut()
    setFeatures({})
    setIsPlatformAdmin(false)
  }, [])

  const value = useMemo(
    () => ({
      configured: supabaseConfigured,
      loading,
      session,
      accessToken: session?.access_token ?? null,
      email,
      features,
      isPlatformAdmin,
      passwordRecovery,
      refreshMe,
      signOut,
    }),
    [loading, session, email, features, isPlatformAdmin, passwordRecovery, refreshMe, signOut],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
