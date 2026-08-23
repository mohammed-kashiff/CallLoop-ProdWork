import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { apiFetch } from '../lib/api'
import type { PyaiStatus } from '../types'

interface PyaiStatusValue {
  status: PyaiStatus | null
  isSandbox: boolean
  isLive: boolean
  label: string
  keysOpen: boolean
  openKeys: () => void
  closeKeys: () => void
  refresh: () => Promise<void>
}

const PyaiStatusContext = createContext<PyaiStatusValue | null>(null)

function deriveLabel(status: PyaiStatus | null): string {
  if (!status) return '…'
  const raw = (status.label || '').trim()
  if (raw) return raw
  if (status.env === 'test') return 'Sandbox'
  if (status.env === 'live') return 'Live'
  return 'PyAI'
}

function deriveSandbox(status: PyaiStatus | null, label: string): boolean {
  if (!status) return false
  if (status.env === 'test') return true
  if (status.env === 'live') return false
  return label.toLowerCase() === 'sandbox'
}

export function PyaiStatusProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<PyaiStatus | null>(null)
  const [keysOpen, setKeysOpen] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const r = await apiFetch('/api/pyai/status')
      const data = (await r.json()) as PyaiStatus
      setStatus(data)
    } catch {
      setStatus({
        ok: false,
        healthy: false,
        label: 'PyAI',
        quota_label: 'Status unavailable',
      })
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    const load = () => {
      if (cancelled) return
      void refresh()
    }
    load()
    const id = window.setInterval(load, 60000)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [refresh])

  const value = useMemo(() => {
    const label = deriveLabel(status)
    const isSandbox = deriveSandbox(status, label)
    return {
      status,
      isSandbox,
      isLive: Boolean(status) && !isSandbox && label.toLowerCase() === 'live',
      label,
      keysOpen,
      openKeys: () => setKeysOpen(true),
      closeKeys: () => setKeysOpen(false),
      refresh,
    }
  }, [status, keysOpen, refresh])

  return (
    <PyaiStatusContext.Provider value={value}>{children}</PyaiStatusContext.Provider>
  )
}

export function usePyaiStatus() {
  const ctx = useContext(PyaiStatusContext)
  if (!ctx) throw new Error('usePyaiStatus must be used within PyaiStatusProvider')
  return ctx
}
