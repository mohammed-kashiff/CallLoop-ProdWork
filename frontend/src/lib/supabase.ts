import { createClient, type SupabaseClient } from '@supabase/supabase-js'

const url = String(import.meta.env.VITE_SUPABASE_URL || '').replace(/\/$/, '')
const anon = String(import.meta.env.VITE_SUPABASE_ANON_KEY || '').trim()

export const supabaseConfigured = Boolean(url && anon)

export const supabase: SupabaseClient | null = supabaseConfigured
  ? createClient(url, anon, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
      },
    })
  : null
