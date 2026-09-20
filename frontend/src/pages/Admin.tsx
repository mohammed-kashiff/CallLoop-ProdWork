import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from 'react'
import { Link, Navigate } from 'react-router-dom'
import { apiFetch, fmtUsd, readError } from '../lib/api'
import type { FeatureMap } from '../lib/features'
import { formatBytes } from '../lib/format'
import { roleTagLabel } from '../lib/roles'
import { supabase, supabaseConfigured } from '../lib/supabase'
import { useAuth } from '../context/AuthContext'
import { CUSTOMER_ORIGIN, isAdminHost } from '../lib/adminHost'
import { CcDenied, CommandCenterPage } from '../components/cc/CommandCenterPage'
import { CcEmpty, CcTable } from '../components/cc/CcTable'
import { CcSearchBar } from '../components/cc/CcSearchBar'
import {
  ccBtn,
  ccErr,
  ccGhost,
  ccHint,
  ccInput,
  ccLabel,
  ccMono,
  ccRow,
  ccTd,
} from '../components/cc/classes'

type ProvisionResult = {
  email: string
  org_name: string
  created: boolean
  temporary_password: string
}

// AC-33's shape: one row per org, not per member.
type OrgRow = {
  org_id: string
  org_name: string | null
  short_ids: number[]
  member_count: number
  created_at: string | null
}

// AC-34's shape: one row per member, used inside the Members tab.
type DirectoryRow = {
  user_id: string
  email: string | null
  first_name: string | null
  last_name: string | null
  role: string | null
  org_id: string
  org_name: string | null
  short_id: number | null
  first_seen: string | null
  last_sign_in_at: string | null
}

type UsagePayload = {
  org_id: string
  usage: {
    total_hits?: number
    total_polls?: number
    by_provider?: Record<
      string,
      { hits?: number; actions?: number; polls?: number; units?: number }
    >
  }
  cost: { pyai_usd: number; claude_usd: number; total_usd: number }
  features: FeatureMap
}

type OrgCallRow = {
  call_id: number
  filename: string | null
  created_at: string | null
  audio_seconds: number | null
  mode: 'pyai' | 'selfhosted'
  audited: boolean
  uploaded_by: string | null
  requested_by: string | null
  deleted: boolean
  deleted_at: string | null
  deleted_by_short_id: number | null
  data_size_bytes: number
}

type OrgDetailPayload = {
  org_id: string
  total_calls: number
  audited_count: number
  calls: OrgCallRow[]
  calls_truncated: boolean
  total_data_size_bytes: number
}

type RubricPayload = {
  org_id: string
  source: 'custom' | 'legacy'
  rubric_id: string | null
  version: number | null
  updated_at: string | null
  weights: Record<string, number>
}

const RUBRIC_DIMENSIONS: { id: string; label: string }[] = [
  { id: 'resolution_effectiveness', label: 'Resolution Effectiveness' },
  { id: 'ownership_next_steps', label: 'Ownership & Next Steps' },
  { id: 'active_listening', label: 'Active Listening' },
  { id: 'tone_empathy_professionalism', label: 'Tone, Empathy & Professionalism' },
]

// AC-35: one point per day, real counts — replaces the design mock's Math.random().
type DailyUsagePoint = {
  date: string
  hits: number
  actions: number
  polls: number
  units: number
}

// AC-36: served by GET /api/admin/feature-flags — a flag's label/description/
// risk tier lives here, not hardcoded per-key in this file.
type RiskTier = 'low' | 'medium' | 'danger'
type FeatureFlagDef = {
  key: string
  label: string
  description: string
  risk: RiskTier
  default_enabled: boolean
}

const RISK_ORDER: RiskTier[] = ['low', 'medium', 'danger']
const RISK_LABEL: Record<RiskTier, string> = {
  low: 'Low risk',
  medium: 'Medium risk',
  danger: 'Danger zone',
}

function isFlagOn(features: FeatureMap | undefined, def: FeatureFlagDef): boolean {
  const value = features?.[def.key]
  if (value === undefined) return def.default_enabled
  return value !== false
}

// Account logs tab: GET /api/admin/orgs/{org_id}/feature-history — every
// flag change for the org, any key, newest first.
type FeatureHistoryEvent = {
  feature_key: string
  enabled: boolean
  changed_by: string
  changed_at: string | null
}

type ActivityEvent = {
  at: string | null
  kind: 'upload' | 'audit' | 'flag_change' | 'delete'
  actor: string | null
  call_id: number | null
  filename: string | null
  feature_key: string | null
  enabled: boolean | null
}

type ActivityPayload = {
  org_id: string
  events: ActivityEvent[]
  truncated: boolean
}

type PasswordEvent = {
  event_type: 'self_service' | 'admin_reset_email' | 'admin_direct_reset'
  actor_email: string | null
  ip_address: string | null
  created_at: string | null
}

function passwordEventLabel(e: PasswordEvent): string {
  if (e.event_type === 'self_service') return 'Self-service'
  if (e.event_type === 'admin_reset_email') return `Admin reset email${e.actor_email ? ` (${e.actor_email})` : ''}`
  return `Admin direct reset${e.actor_email ? ` (${e.actor_email})` : ''}`
}

function ymd(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function weekAgo(): string {
  const d = new Date()
  d.setDate(d.getDate() - 7)
  return ymd(d)
}

function activityLabel(e: ActivityEvent): string {
  if (e.kind === 'upload') return e.filename || (e.call_id != null ? `Call ${e.call_id}` : 'Upload')
  if (e.kind === 'audit') return e.call_id != null ? `Call ${e.call_id}` : 'Audit'
  if (e.kind === 'delete') return e.filename || (e.call_id != null ? `Call ${e.call_id}` : 'Delete')
  const flag = e.feature_key || 'flag'
  if (e.enabled === true) return `${flag} on`
  if (e.enabled === false) return `${flag} off`
  return flag
}

function activityKind(kind: ActivityEvent['kind']): string {
  if (kind === 'upload') return 'Upload'
  if (kind === 'audit') return 'Audit'
  if (kind === 'delete') return 'Delete'
  return 'Flag'
}

function displayName(row: DirectoryRow): string {
  const n = [row.first_name, row.last_name].filter(Boolean).join(' ').trim()
  return n || '—'
}

/** AC-37/AC-40: one reusable overlay for the danger-toggle confirmation and
 * the Provision-user dialog — Escape and backdrop-click both close it. */
function Modal({
  title,
  onClose,
  children,
}: {
  title: string
  onClose: () => void
  children: ReactNode
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-lg border border-cc-line bg-cc-card p-5 text-cc-ink shadow-lg"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-start justify-between gap-3">
          <h3 className="text-base font-semibold">{title}</h3>
          <button type="button" className="text-xl leading-none text-cc-muted" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}

/** AC-38: real per-day hits, no mock random numbers — a plain inline SVG
 * line, no charting dependency. */
function UsageSparkline({ data }: { data: DailyUsagePoint[] }) {
  if (data.length === 0) return <p className={ccHint}>No usage data yet.</p>
  const w = 640
  const h = 200
  const padX = 12
  const padTop = 14
  const padBottom = 28
  const max = Math.max(1, ...data.map((d) => d.hits))
  const stepX = data.length > 1 ? (w - padX * 2) / (data.length - 1) : 0
  const xAt = (i: number) => padX + i * stepX
  const yAt = (hits: number) =>
    h - padBottom - (hits / max) * (h - padBottom - padTop)
  const linePoints = data.map((d, i) => `${xAt(i).toFixed(1)},${yAt(d.hits).toFixed(1)}`).join(' ')
  const areaPoints = `${padX},${h - padBottom} ${linePoints} ${xAt(data.length - 1).toFixed(1)},${h - padBottom}`
  const gridLines = [0, 0.25, 0.5, 0.75, 1]
  const total = data.reduce((sum, d) => sum + d.hits, 0)
  // A handful of evenly-spaced date labels rather than one per day — 30
  // labels would overlap into an unreadable smear.
  const labelEvery = Math.max(1, Math.ceil(data.length / 6))

  return (
    <figure className="text-cc-muted">
      <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label="Daily API hits trend, last 30 days">
        <defs>
          <linearGradient id="cc-chart-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.2" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
          </linearGradient>
        </defs>
        {gridLines.map((g) => (
          <line
            key={g}
            x1={padX}
            x2={w - padX}
            y1={padTop + g * (h - padBottom - padTop)}
            y2={padTop + g * (h - padBottom - padTop)}
            stroke="currentColor"
            strokeWidth="1"
          />
        ))}
        <polygon points={areaPoints} fill="url(#cc-chart-fill)" stroke="none" />
        <polyline points={linePoints} fill="none" stroke="currentColor" strokeWidth="2" />
        {data.map((d, i) =>
          i % labelEvery === 0 ? (
            <text
              key={d.date}
              x={xAt(i)}
              y={h - 8}
              fontSize="10"
              textAnchor="middle"
              fill="currentColor"
            >
              {d.date.slice(5)}
            </text>
          ) : null,
        )}
      </svg>
      <figcaption className={ccHint}>
        {data[0].date} – {data[data.length - 1].date}: {total} hits total
      </figcaption>
    </figure>
  )
}

export function Admin() {
  const { isPlatformAdmin } = useAuth()

  // AC-39: real search, org-level (AC-33) since the directory table below
  // is now one row per org, not per member. ⌘K focuses this input.
  const [orgQuery, setOrgQuery] = useState('')
  const [orgRows, setOrgRows] = useState<OrgRow[]>([])
  const [orgSearchError, setOrgSearchError] = useState<string | null>(null)
  const searchInputRef = useRef<HTMLInputElement | null>(null)

  // AC-37: the slide-over inspector. selectedOrg drives whether it's open —
  // no route change, so the table underneath never unmounts and its scroll
  // position is preserved automatically when the drawer closes.
  const [selectedOrg, setSelectedOrg] = useState<OrgRow | null>(null)
  const [activeTab, setActiveTab] = useState<
    'overview' | 'flags' | 'rubric' | 'members' | 'account-logs'
  >(
    'overview',
  )

  const [usage, setUsage] = useState<UsagePayload | null>(null)
  const [orgDetail, setOrgDetail] = useState<OrgDetailPayload | null>(null)
  const [busy, setBusy] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)

  // AC-35
  const [daily, setDaily] = useState<DailyUsagePoint[]>([])

  // AC-36
  const [flagDefs, setFlagDefs] = useState<FeatureFlagDef[]>([])
  const [pendingDangerToggle, setPendingDangerToggle] = useState<{
    def: FeatureFlagDef
    nextEnabled: boolean
  } | null>(null)

  // Small bottom-left "Saved" confirmation after a flag toggle or rubric save.
  const [toast, setToast] = useState<string | null>(null)
  const toastTimer = useRef<number | null>(null)
  const showToast = (message: string) => {
    setToast(message)
    if (toastTimer.current) window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(null), 2500)
  }

  const [copiedOrgId, setCopiedOrgId] = useState(false)
  const copyOrgId = async (orgId: string) => {
    try {
      await navigator.clipboard.writeText(orgId)
      setCopiedOrgId(true)
      window.setTimeout(() => setCopiedOrgId(false), 1500)
    } catch {
      /* clipboard permission denied — not worth surfacing an error for */
    }
  }

  // Account logs tab: every flag change for this org (any key), merged
  // with each real member's join date already fetched for the Members tab.
  const [featureHistory, setFeatureHistory] = useState<FeatureHistoryEvent[]>([])

  const [actOrg, setActOrg] = useState('')
  const [actSince, setActSince] = useState(weekAgo)
  const [actUntil, setActUntil] = useState(() => ymd(new Date()))
  const [activity, setActivity] = useState<ActivityPayload | null>(null)
  const [actBusy, setActBusy] = useState(false)
  const [actError, setActError] = useState<string | null>(null)

  // AC-40: same fields/endpoint as before, now behind a modal.
  const [provisionModalOpen, setProvisionModalOpen] = useState(false)
  const [pEmail, setPEmail] = useState('')
  const [pFirst, setPFirst] = useState('')
  const [pLast, setPLast] = useState('')
  const [pOrgName, setPOrgName] = useState('')
  const [provisioning, setProvisioning] = useState(false)
  const [provisionError, setProvisionError] = useState<string | null>(null)
  const [provisionResult, setProvisionResult] = useState<ProvisionResult | null>(null)
  const [copied, setCopied] = useState(false)

  // AC-34 Members tab: every real member of the selected org, each with
  // their own independently-clickable Log in as / Send reset email /
  // History — keyed by user_id so one member's state never touches another's.
  const [orgMembers, setOrgMembers] = useState<DirectoryRow[]>([])
  const [membersError, setMembersError] = useState<string | null>(null)
  const [impersonatingMemberId, setImpersonatingMemberId] = useState<string | null>(null)
  const [memberImpersonateErrors, setMemberImpersonateErrors] = useState<
    Record<string, string>
  >({})
  const [resettingMemberId, setResettingMemberId] = useState<string | null>(null)
  const [memberResetInfo, setMemberResetInfo] = useState<Record<string, string>>({})
  const [memberResetErrors, setMemberResetErrors] = useState<Record<string, string>>({})
  const [expandedMemberId, setExpandedMemberId] = useState<string | null>(null)
  const [pwEventsByUser, setPwEventsByUser] = useState<
    Record<string, PasswordEvent[] | null>
  >({})

  const [rubric, setRubric] = useState<RubricPayload | null>(null)
  const [rubricDraft, setRubricDraft] = useState<Record<string, number>>({})
  const [rubricSaving, setRubricSaving] = useState(false)
  const [rubricError, setRubricError] = useState<string | null>(null)
  const [rubricSaveInfo, setRubricSaveInfo] = useState<string | null>(null)

  const searchOrgs = useCallback(async (needle: string) => {
    const r = await apiFetch(`/api/admin/orgs?q=${encodeURIComponent(needle)}`)
    if (!r.ok) throw new Error(await readError(r, 'Could not search the directory.'))
    const data = (await r.json()) as { rows?: OrgRow[] }
    setOrgRows(Array.isArray(data.rows) ? data.rows : [])
  }, [])

  useEffect(() => {
    if (!isPlatformAdmin) return
    const t = window.setTimeout(() => {
      searchOrgs(orgQuery).catch((e: unknown) =>
        setOrgSearchError(e instanceof Error ? e.message : 'Could not search the directory.'),
      )
    }, 250)
    return () => window.clearTimeout(t)
  }, [orgQuery, isPlatformAdmin, searchOrgs])

  // AC-36: fetch the risk-tier metadata once — it doesn't change per org.
  useEffect(() => {
    if (!isPlatformAdmin) return
    apiFetch('/api/admin/feature-flags')
      .then((r) => (r.ok ? r.json() : { flags: [] }))
      .then((data: { flags?: FeatureFlagDef[] }) =>
        setFlagDefs(Array.isArray(data.flags) ? data.flags : []),
      )
      .catch(() => setFlagDefs([]))
  }, [isPlatformAdmin])

  // AC-39: ⌘K / Ctrl+K focuses the search bar from anywhere on the page.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        searchInputRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const provisionUser = async (e: FormEvent) => {
    e.preventDefault()
    setProvisionError(null)
    setProvisionResult(null)
    setCopied(false)
    setProvisioning(true)
    try {
      const r = await apiFetch('/api/admin/provision-user', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: pEmail.trim(),
          first_name: pFirst.trim(),
          last_name: pLast.trim(),
          org_mode: 'new',
          org_name: pOrgName.trim(),
        }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not provision that user.'))
      const data = (await r.json()) as ProvisionResult
      setProvisionResult(data)
      setPEmail('')
      setPFirst('')
      setPLast('')
      setPOrgName('')
      searchOrgs(orgQuery).catch(() => {})
    } catch (err: unknown) {
      setProvisionError(err instanceof Error ? err.message : 'Could not provision that user.')
    } finally {
      setProvisioning(false)
    }
  }

  const copyPassword = async () => {
    if (!provisionResult) return
    try {
      await navigator.clipboard.writeText(provisionResult.temporary_password)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  const closeProvisionModal = () => {
    setProvisionModalOpen(false)
    setProvisionError(null)
    setProvisionResult(null)
    setCopied(false)
  }

  const loadOrgMembers = async (orgId: string) => {
    try {
      // Reuses the existing per-member directory search as-is (AC-34)
      // rather than a new endpoint — org_id is a UUID, so a substring
      // match against it can only ever match that one org's rows.
      const r = await apiFetch(`/api/admin/directory?q=${encodeURIComponent(orgId)}`)
      if (!r.ok) throw new Error(await readError(r, 'Could not load org members.'))
      const data = (await r.json()) as { rows?: DirectoryRow[] }
      setOrgMembers(Array.isArray(data.rows) ? data.rows.filter((m) => m.org_id === orgId) : [])
    } catch (e: unknown) {
      setOrgMembers([])
      setMembersError(e instanceof Error ? e.message : 'Could not load org members.')
    }
  }

  // AC-37: opening an org fetches everything its 4 tabs need up front, so
  // switching tabs inside the drawer is instant (no per-tab loading state).
  const openOrg = async (row: OrgRow) => {
    setSelectedOrg(row)
    setActiveTab('overview')
    setDetailError(null)
    setRubric(null)
    setRubricError(null)
    setRubricSaveInfo(null)
    setOrgMembers([])
    setMembersError(null)
    setMemberImpersonateErrors({})
    setMemberResetInfo({})
    setMemberResetErrors({})
    setExpandedMemberId(null)
    setPwEventsByUser({})
    setDaily([])
    setFeatureHistory([])
    setBusy(true)
    try {
      const [usageRes, detailRes, rubricRes, dailyRes, historyRes] = await Promise.all([
        apiFetch(`/api/admin/usage?org_id=${encodeURIComponent(row.org_id)}`),
        apiFetch(`/api/admin/orgs/${encodeURIComponent(row.org_id)}/detail`),
        apiFetch(`/api/admin/orgs/${encodeURIComponent(row.org_id)}/rubric`),
        apiFetch(`/api/admin/usage/daily?org_id=${encodeURIComponent(row.org_id)}&days=30`),
        apiFetch(`/api/admin/orgs/${encodeURIComponent(row.org_id)}/feature-history`),
      ])
      if (!usageRes.ok) throw new Error(await readError(usageRes, 'Could not load usage.'))
      if (!detailRes.ok) throw new Error(await readError(detailRes, 'Could not load org detail.'))
      setUsage((await usageRes.json()) as UsagePayload)
      setOrgDetail((await detailRes.json()) as OrgDetailPayload)
      if (rubricRes.ok) {
        const data = (await rubricRes.json()) as RubricPayload
        setRubric(data)
        setRubricDraft(data.weights)
      } else {
        setRubricError(await readError(rubricRes, 'Could not load the rubric.'))
      }
      if (dailyRes.ok) {
        const data = (await dailyRes.json()) as { series?: DailyUsagePoint[] }
        setDaily(Array.isArray(data.series) ? data.series : [])
      }
      if (historyRes.ok) {
        const data = (await historyRes.json()) as { events?: FeatureHistoryEvent[] }
        setFeatureHistory(Array.isArray(data.events) ? data.events : [])
      }
    } catch (e: unknown) {
      setUsage(null)
      setOrgDetail(null)
      setDetailError(e instanceof Error ? e.message : 'Could not load usage.')
    } finally {
      setBusy(false)
    }
    void loadOrgMembers(row.org_id)
  }

  const closeDrawer = () => setSelectedOrg(null)

  const rubricTotal = RUBRIC_DIMENSIONS.reduce(
    (sum, dim) => sum + (rubricDraft[dim.id] ?? 0),
    0,
  )

  const saveRubric = async () => {
    if (!selectedOrg || rubricTotal !== 100) return
    setRubricSaving(true)
    setRubricError(null)
    setRubricSaveInfo(null)
    try {
      const r = await apiFetch(`/api/admin/orgs/${encodeURIComponent(selectedOrg.org_id)}/rubric`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ weights: rubricDraft }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not save the rubric.'))
      const data = (await r.json()) as RubricPayload
      setRubric(data)
      setRubricDraft(data.weights)
      setRubricSaveInfo(`Saved — version ${data.version} active.`)
      showToast('Saved')
    } catch (e: unknown) {
      setRubricError(e instanceof Error ? e.message : 'Could not save the rubric.')
    } finally {
      setRubricSaving(false)
    }
  }

  const loadActivity = async (e: FormEvent) => {
    e.preventDefault()
    setActError(null)
    setActBusy(true)
    try {
      const ref = actOrg.trim()
      const params = new URLSearchParams({ since: actSince, until: actUntil })
      if (/^\d+$/.test(ref)) params.set('short_id', ref)
      else params.set('org_id', ref)
      const r = await apiFetch(`/api/admin/activity?${params.toString()}`)
      if (!r.ok) throw new Error(await readError(r, 'Could not load activity.'))
      setActivity((await r.json()) as ActivityPayload)
    } catch (err: unknown) {
      setActivity(null)
      setActError(err instanceof Error ? err.message : 'Could not load activity.')
    } finally {
      setActBusy(false)
    }
  }

  // AC-36: the actual write, only ever called after a danger-tier toggle
  // has been explicitly confirmed (or immediately for low/medium).
  const applyToggle = async (key: string, enabled: boolean) => {
    if (!selectedOrg) return
    setDetailError(null)
    const r = await apiFetch('/api/admin/features', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        org_id: selectedOrg.org_id,
        feature_key: key,
        enabled,
      }),
    })
    if (!r.ok) {
      setDetailError(await readError(r, 'Could not update that flag.'))
      return
    }
    const data = (await r.json()) as { features?: FeatureMap }
    setUsage((prev) =>
      prev ? { ...prev, features: data.features || prev.features } : prev,
    )
    showToast('Saved')
    // Refresh Account logs so the change shows up there immediately.
    try {
      const historyRes = await apiFetch(
        `/api/admin/orgs/${encodeURIComponent(selectedOrg.org_id)}/feature-history`,
      )
      if (historyRes.ok) {
        const historyData = (await historyRes.json()) as { events?: FeatureHistoryEvent[] }
        setFeatureHistory(Array.isArray(historyData.events) ? historyData.events : [])
      }
    } catch {
      /* Account logs will just be one entry stale until the next open — not worth surfacing */
    }
  }

  const requestToggle = (def: FeatureFlagDef, nextEnabled: boolean) => {
    if (def.risk === 'danger') {
      setPendingDangerToggle({ def, nextEnabled })
      return
    }
    void applyToggle(def.key, nextEnabled)
  }

  const confirmDangerToggle = () => {
    if (!pendingDangerToggle) return
    void applyToggle(pendingDangerToggle.def.key, pendingDangerToggle.nextEnabled)
    setPendingDangerToggle(null)
  }

  const logInAsMember = async (row: DirectoryRow) => {
    setMemberImpersonateErrors((prev) => {
      const next = { ...prev }
      delete next[row.user_id]
      return next
    })
    setImpersonatingMemberId(row.user_id)
    try {
      const r = await apiFetch(`/api/admin/users/${encodeURIComponent(row.user_id)}/impersonate`, {
        method: 'POST',
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not start impersonation session.'))
      const body = (await r.json()) as {
        org_name: string | null
        target_email: string
        access_token: string
        refresh_token: string
        expires_in: number | null
        token_type: string
      }
      const hash = new URLSearchParams({
        access_token: body.access_token,
        refresh_token: body.refresh_token,
        token_type: body.token_type || 'bearer',
        type: 'magiclink',
        ...(body.expires_in ? { expires_in: String(body.expires_in) } : {}),
      })
      const query = new URLSearchParams({
        impersonated: '1',
        org: body.org_name || row.org_name || 'this org',
        as: body.target_email,
      })
      window.open(`${CUSTOMER_ORIGIN}/?${query.toString()}#${hash.toString()}`, '_blank')
    } catch (e) {
      setMemberImpersonateErrors((prev) => ({
        ...prev,
        [row.user_id]:
          e instanceof Error ? e.message : 'Could not start impersonation session.',
      }))
    } finally {
      setImpersonatingMemberId((current) => (current === row.user_id ? null : current))
    }
  }

  const sendResetEmailForMember = async (row: DirectoryRow) => {
    if (!row.email) return
    setMemberResetErrors((prev) => {
      const next = { ...prev }
      delete next[row.user_id]
      return next
    })
    setMemberResetInfo((prev) => {
      const next = { ...prev }
      delete next[row.user_id]
      return next
    })
    if (!supabase) {
      setMemberResetErrors((prev) => ({ ...prev, [row.user_id]: 'Auth is not configured.' }))
      return
    }
    setResettingMemberId(row.user_id)
    try {
      const { error: err } = await supabase.auth.resetPasswordForEmail(row.email.trim(), {
        redirectTo: `${window.location.origin}/reset-password`,
      })
      if (err) {
        const msg = err.message.toLowerCase()
        const leaky = /not found|does not exist|no user|unregistered|could not find/.test(msg)
        if (!leaky) {
          setMemberResetErrors((prev) => ({ ...prev, [row.user_id]: err.message }))
          return
        }
      }
      setMemberResetInfo((prev) => ({ ...prev, [row.user_id]: 'Reset email sent.' }))
      try {
        await apiFetch('/api/admin/log-password-reset-request', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ user_id: row.user_id, email: row.email }),
        })
      } catch {
        /* email already sent; a log miss must not look like a failed reset */
      }
      if (pwEventsByUser[row.user_id] !== undefined) {
        setPwEventsByUser((prev) => {
          const next = { ...prev }
          delete next[row.user_id]
          return next
        })
      }
    } catch {
      setMemberResetErrors((prev) => ({
        ...prev,
        [row.user_id]: 'Could not send the reset email.',
      }))
    } finally {
      setResettingMemberId((current) => (current === row.user_id ? null : current))
    }
  }

  const toggleMemberHistory = async (row: DirectoryRow) => {
    if (expandedMemberId === row.user_id) {
      setExpandedMemberId(null)
      return
    }
    setExpandedMemberId(row.user_id)
    if (pwEventsByUser[row.user_id] !== undefined) return
    try {
      const r = await apiFetch(`/api/admin/users/${encodeURIComponent(row.user_id)}/password-events`)
      if (!r.ok) throw new Error(await readError(r, 'Could not load password history.'))
      const data = (await r.json()) as { events?: PasswordEvent[] }
      setPwEventsByUser((prev) => ({
        ...prev,
        [row.user_id]: Array.isArray(data.events) ? data.events : [],
      }))
    } catch {
      setPwEventsByUser((prev) => ({ ...prev, [row.user_id]: null }))
    }
  }

  if (!isPlatformAdmin) {
    if (isAdminHost()) return <CcDenied title="Command Center" />
    return <Navigate to="/" replace />
  }

  const pyai = usage?.usage?.by_provider?.pyai
  const claude = usage?.usage?.by_provider?.anthropic
  const flagsByRisk = RISK_ORDER.map((risk) => ({
    risk,
    defs: flagDefs.filter((d) => d.risk === risk),
  })).filter((g) => g.defs.length > 0)

  // Account logs tab: flag changes + real member joins, one merged
  // chronological list — "what feature was enabled/disabled and when,
  // when each team member joined."
  type AccountLogEntry = { at: string; summary: string; detail: string }
  const accountLog: AccountLogEntry[] = [
    ...featureHistory
      .filter((h) => h.changed_at)
      .map((h) => ({
        at: h.changed_at as string,
        summary: `Flag ${h.enabled ? 'enabled' : 'disabled'}`,
        detail: `${h.feature_key} — ${h.changed_by || 'unknown admin'}`,
      })),
    ...orgMembers
      .filter((m) => m.first_seen)
      .map((m) => ({
        at: m.first_seen as string,
        summary: 'Member joined',
        detail: `${displayName(m)} (${m.email || m.user_id}) joined CallLoop`,
      })),
  ].sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime())

  return (
    <CommandCenterPage
      title="Command Center"
      crumb="Platform"
      actions={
        <button type="button" className={ccBtn} onClick={() => setProvisionModalOpen(true)}>
          Provision user
        </button>
      }
    >
      <CcSearchBar
        value={orgQuery}
        onChange={setOrgQuery}
        placeholder="Search orgs — email, name, org id, short id (⌘K)"
        hideSubmit
        inputRef={searchInputRef}
      />

      {orgSearchError ? (
        <p className={ccErr} role="alert">
          {orgSearchError}
        </p>
      ) : null}

      <div className="mb-8">
        <div className="mb-3 flex items-baseline justify-between gap-3">
          <h2 className="text-base font-semibold text-cc-ink">Organizations</h2>
          <span className="text-[13px] text-cc-muted">
            {orgRows.length} {orgRows.length === 1 ? 'organization' : 'organizations'}
          </span>
        </div>
        {orgRows.length === 0 ? (
          <CcEmpty title="No matching orgs" body="Try a different email, name, or id." />
        ) : (
          <CcTable columns={['Org', 'Members', 'Short IDs', 'Created']}>
            {orgRows.map((row) => (
              <tr
                key={row.org_id}
                className={`${ccRow} cursor-pointer ${selectedOrg?.org_id === row.org_id ? 'bg-cc-paper' : ''}`}
                onClick={() => void openOrg(row)}
              >
                <td className={ccTd}>
                  <div className="flex items-center gap-3">
                    <span
                      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-cc-paper text-[13px] font-semibold"
                      aria-hidden="true"
                    >
                      {(row.org_name || '?').slice(0, 1).toUpperCase()}
                    </span>
                    <span>
                      <span className="block font-medium">{row.org_name || '—'}</span>
                      <span className={ccMono}>{row.org_id}</span>
                    </span>
                  </div>
                </td>
                <td className={ccTd}>{row.member_count}</td>
                <td className={ccTd}>{row.short_ids.length > 0 ? row.short_ids.join(', ') : '—'}</td>
                <td className={ccTd}>
                  {row.created_at ? new Date(row.created_at).toLocaleDateString() : '—'}
                </td>
              </tr>
            ))}
          </CcTable>
        )}
      </div>

      <details className="mb-8 rounded-lg border border-cc-line bg-cc-card p-5">
        <summary className="cursor-pointer text-base font-semibold text-cc-ink">Org activity</summary>
        <p className={`${ccHint} mt-2 mb-4`}>
          Uploads, audits, and flag changes for one org in a date range.
          Retranscribes show as a new audit on that call. Not application logs.
        </p>
        <form className="flex flex-wrap items-end gap-3" onSubmit={(e) => void loadActivity(e)}>
          <label className={ccLabel}>
            Org id or short id
            <input
              type="text"
              className={ccInput}
              value={actOrg}
              onChange={(e) => setActOrg(e.target.value)}
              placeholder="UUID or 100001"
              required
            />
          </label>
          <label className={ccLabel}>
            From
            <input
              type="date"
              className={ccInput}
              value={actSince}
              onChange={(e) => setActSince(e.target.value)}
              required
            />
          </label>
          <label className={ccLabel}>
            To
            <input
              type="date"
              className={ccInput}
              value={actUntil}
              onChange={(e) => setActUntil(e.target.value)}
              required
            />
          </label>
          <button type="submit" className={ccBtn} disabled={actBusy}>
            {actBusy ? 'Loading…' : 'Load'}
          </button>
        </form>
        {actError ? (
          <p className={`${ccErr} mt-3`} role="alert">
            {actError}
          </p>
        ) : null}
        {activity ? (
          <div className="mt-4">
            {activity.events.length === 0 ? (
              <CcEmpty title="No activity in that window" />
            ) : (
              <CcTable
                columns={['When', 'Type', 'Who', 'Detail']}
                footer={
                  activity.truncated ? (
                    <p className={ccHint}>Showing the most recent rows in this range.</p>
                  ) : null
                }
              >
                {activity.events.map((ev, i) => (
                  <tr key={`${ev.kind}-${ev.at}-${ev.call_id ?? ev.feature_key ?? i}`} className={ccRow}>
                    <td className={ccTd}>{ev.at ? new Date(ev.at).toLocaleString() : '—'}</td>
                    <td className={ccTd}>{activityKind(ev.kind)}</td>
                    <td className={ccTd}>{ev.actor || '—'}</td>
                    <td className={ccTd}>{activityLabel(ev)}</td>
                  </tr>
                ))}
              </CcTable>
            )}
          </div>
        ) : null}
      </details>

      {/* AC-37: slide-over inspector. Backdrop click or Escape closes it. */}
      {selectedOrg ? (
        <div className="fixed inset-0 z-40 bg-black/40" onClick={closeDrawer}>
          <aside
            className="absolute right-0 top-0 flex h-full w-full max-w-[min(40rem,100%)] flex-col overflow-y-auto border-l border-cc-line bg-cc-card p-6 text-cc-ink shadow-xl"
            role="dialog"
            aria-modal="true"
            aria-label={selectedOrg.org_name || 'Organization'}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-4 flex items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold">{selectedOrg.org_name || 'Organization'}</h2>
                <div className="mt-1 flex items-center gap-2">
                  <p className={ccMono}>{selectedOrg.org_id}</p>
                  <button
                    type="button"
                    className="text-[12px] font-semibold text-cc-muted hover:text-cc-ink"
                    onClick={() => void copyOrgId(selectedOrg.org_id)}
                  >
                    {copiedOrgId ? 'Copied' : 'Copy'}
                  </button>
                </div>
              </div>
              <button type="button" className="text-xl leading-none text-cc-muted" onClick={closeDrawer} aria-label="Close">
                ×
              </button>
            </div>

            <nav className="mb-5 flex flex-wrap gap-1 border-b border-cc-line" aria-label="Org detail tabs">
              {(['overview', 'flags', 'rubric', 'members', 'account-logs'] as const).map((tab) => (
                <button
                  key={tab}
                  type="button"
                  className={`-mb-px border-b-2 px-3 py-2 text-[13px] font-semibold ${
                    activeTab === tab
                      ? 'border-cc-ink text-cc-ink'
                      : 'border-transparent text-cc-muted hover:text-cc-ink'
                  }`}
                  onClick={() => setActiveTab(tab)}
                >
                  {tab === 'overview'
                    ? 'Overview'
                    : tab === 'flags'
                      ? 'Flags'
                      : tab === 'rubric'
                        ? 'Rubric'
                        : tab === 'members'
                          ? 'Members'
                          : 'Account logs'}
                </button>
              ))}
            </nav>

            {detailError ? (
              <p className={ccErr} role="alert">
                {detailError}
              </p>
            ) : null}
            {busy ? <p className={ccHint}>Loading…</p> : null}

            <div className="grid gap-5">
              {activeTab === 'overview' ? (
                <div className="grid gap-4">
                  <dl className="grid grid-cols-2 gap-3 text-[13px]">
                    <div>
                      <dt className="text-cc-muted">Members</dt>
                      <dd className="text-lg font-semibold">{selectedOrg.member_count}</dd>
                    </div>
                    <div>
                      <dt className="text-cc-muted">Created</dt>
                      <dd className="text-lg font-semibold">
                        {selectedOrg.created_at
                          ? new Date(selectedOrg.created_at).toLocaleDateString()
                          : '—'}
                      </dd>
                    </div>
                  </dl>

                  {usage || orgDetail ? (
                    <div className="grid grid-cols-2 gap-3">
                      {usage ? (
                        <>
                          <div className="rounded-md border border-cc-line p-3">
                            <p className="text-[11px] font-semibold uppercase tracking-wide text-cc-muted">PyAI calls</p>
                            <p className="text-xl font-semibold">{pyai?.hits ?? 0}</p>
                          </div>
                          <div className="rounded-md border border-cc-line p-3">
                            <p className="text-[11px] font-semibold uppercase tracking-wide text-cc-muted">Anthropic calls</p>
                            <p className="text-xl font-semibold">{claude?.hits ?? 0}</p>
                          </div>
                          <div className="rounded-md border border-cc-line p-3">
                            <p className="text-[11px] font-semibold uppercase tracking-wide text-cc-muted">Est. spend</p>
                            <p className="text-xl font-semibold">{fmtUsd(usage.cost.total_usd)}</p>
                          </div>
                        </>
                      ) : null}
                      {orgDetail ? (
                        <div className="rounded-md border border-cc-line p-3">
                          <p className="text-[11px] font-semibold uppercase tracking-wide text-cc-muted">Data stored</p>
                          <p className="text-xl font-semibold">{formatBytes(orgDetail.total_data_size_bytes)}</p>
                        </div>
                      ) : null}
                    </div>
                  ) : null}

                  {orgDetail ? (
                    <dl className="grid grid-cols-3 gap-3 text-[13px]">
                      <div>
                        <dt className="text-cc-muted">Total calls</dt>
                        <dd className="font-semibold">{orgDetail.total_calls}</dd>
                      </div>
                      <div>
                        <dt className="text-cc-muted">Audited</dt>
                        <dd className="font-semibold">{orgDetail.audited_count}</dd>
                      </div>
                      <div>
                        <dt className="text-cc-muted">PyAI polls</dt>
                        <dd className="font-semibold">{pyai?.polls ?? 0}</dd>
                      </div>
                    </dl>
                  ) : null}

                  <div className="flex flex-wrap gap-3">
                    <Link
                      className="text-[13px] font-semibold underline-offset-2 hover:underline"
                      to={`/call-logs?query=${encodeURIComponent(selectedOrg.org_id)}`}
                    >
                      Open call logs
                    </Link>
                    <Link
                      className="text-[13px] font-semibold underline-offset-2 hover:underline"
                      to={`/ticket-logs?query=${encodeURIComponent(selectedOrg.org_id)}`}
                    >
                      Open ticket logs
                    </Link>
                  </div>

                  <h3 className="text-sm font-semibold">Usage trend — last 30 days</h3>
                  <UsageSparkline data={daily} />
                </div>
              ) : null}

              {activeTab === 'flags' ? (
                <div>
                    {flagsByRisk.length === 0 ? (
                    <p className={ccHint}>Loading flag definitions…</p>
                  ) : (
                    flagsByRisk.map(({ risk, defs }) => (
                      <div
                        key={risk}
                        className={`mb-4 rounded-md border p-3 ${
                          risk === 'danger'
                            ? 'border-cc-fail/40'
                            : risk === 'medium'
                              ? 'border-cc-line'
                              : 'border-cc-line'
                        }`}
                      >
                        <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-cc-muted">
                          {RISK_LABEL[risk]}
                        </h4>
                        <ul className="admin-flags">
                          {defs.map((def) => {
                            const on = isFlagOn(usage?.features, def)
                            return (
                              <li key={def.key}>
                                <div className="admin-flag-row">
                                  <span className="admin-flag-label">{def.label}</span>
                                  <span className="toggle-switch">
                                    <input
                                      type="checkbox"
                                      checked={on}
                                      disabled={!usage || busy}
                                      onChange={(e) => requestToggle(def, e.target.checked)}
                                      aria-label={def.label}
                                    />
                                    <span className="toggle-track" />
                                    <span className="toggle-thumb" />
                                  </span>
                                </div>
                                {def.description ? (
                                  <p className={ccHint}>{def.description}</p>
                                ) : null}
                              </li>
                            )
                          })}
                        </ul>
                      </div>
                    ))
                  )}
                </div>
              ) : null}

              {activeTab === 'rubric' ? (
                <div>
                  {rubricError ? (
                    <p className={ccErr} role="alert">
                      {rubricError}
                    </p>
                  ) : null}
                  {rubric ? (
                    <>
                      <div className="mb-4 flex items-start justify-between gap-3">
                        <div>
                          <p className="text-sm font-semibold">
                            {rubric.source === 'custom'
                              ? `Custom — version ${rubric.version}`
                              : 'Not yet customized'}
                          </p>
                          <p className={ccHint}>
                            {rubric.updated_at
                              ? `Updated ${new Date(rubric.updated_at).toLocaleString()}`
                              : 'Showing default weights'}
                          </p>
                        </div>
                        <div
                          className={`text-2xl font-semibold ${
                            rubricTotal === 100
                              ? 'text-cc-pass'
                              : rubricTotal > 100
                                ? 'text-cc-fail'
                                : 'text-cc-muted'
                          }`}
                        >
                          {rubricTotal}
                        </div>
                      </div>
                      <div className="admin-rubric-grid">
                        {RUBRIC_DIMENSIONS.map((dim) => (
                          <label key={dim.id} className="admin-rubric-field">
                            <span>{dim.label}</span>
                            <input
                              type="number"
                              min={0}
                              max={100}
                              value={rubricDraft[dim.id] ?? 0}
                              disabled={rubricSaving}
                              onChange={(e) =>
                                setRubricDraft((prev) => ({
                                  ...prev,
                                  [dim.id]: Number(e.target.value) || 0,
                                }))
                              }
                            />
                          </label>
                        ))}
                      </div>
                      <p
                        className={
                          rubricTotal === 100 ? 'admin-rubric-total' : 'admin-rubric-total is-off'
                        }
                      >
                        Total: {rubricTotal} / 100
                      </p>
                      <button
                        type="button"
                        className={ccBtn}
                        disabled={rubricTotal !== 100 || rubricSaving}
                        onClick={() => void saveRubric()}
                      >
                        {rubricSaving ? 'Saving…' : 'Save'}
                      </button>
                      {rubricSaveInfo ? (
                        <p className="auth-info" role="status">
                          {rubricSaveInfo}
                        </p>
                      ) : null}
                    </>
                  ) : !rubricError ? (
                    <p className="empty-copy">Loading…</p>
                  ) : null}
                </div>
              ) : null}

              {activeTab === 'members' ? (
                <div>
                  <p className={`${ccHint} mb-4`}>
                    Every real member of this org — each has their own "Log
                    in as," never ambiguous about who's being impersonated.
                  </p>
                  {membersError ? (
                    <p className={ccErr} role="alert">
                      {membersError}
                    </p>
                  ) : null}
                  {orgMembers.length > 0 ? (
                    <ul className="grid gap-3">
                      {orgMembers.map((m) => (
                        <li key={m.user_id} className="rounded-lg border border-cc-line p-3">
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <p className="truncate font-semibold">{displayName(m)}</p>
                              <p className="truncate text-[13px] text-cc-muted">{m.email || '—'}</p>
                              <p className={`${ccMono} mt-1`}>
                                {m.short_id ?? '—'}
                                {m.first_seen
                                  ? ` · ${new Date(m.first_seen).toLocaleDateString()}`
                                  : ''}
                              </p>
                            </div>
                            <span className="shrink-0 rounded-full border border-cc-line px-2 py-0.5 text-[11px] font-semibold">
                              {roleTagLabel(m.role)}
                            </span>
                          </div>
                          <div className="mt-3 flex flex-wrap gap-2">
                            <button
                              type="button"
                              className={`${ccGhost} whitespace-nowrap`}
                              disabled={impersonatingMemberId === m.user_id || !m.email}
                              onClick={() => void logInAsMember(m)}
                            >
                              {impersonatingMemberId === m.user_id ? 'Starting…' : 'Log in as'}
                            </button>
                            <button
                              type="button"
                              className={`${ccGhost} whitespace-nowrap`}
                              disabled={
                                resettingMemberId === m.user_id ||
                                !m.email ||
                                !supabaseConfigured
                              }
                              onClick={() => void sendResetEmailForMember(m)}
                            >
                              {resettingMemberId === m.user_id ? 'Sending…' : 'Reset email'}
                            </button>
                            <button
                              type="button"
                              className={`${ccGhost} whitespace-nowrap`}
                              onClick={() => void toggleMemberHistory(m)}
                            >
                              {expandedMemberId === m.user_id ? 'Hide history' : 'History'}
                            </button>
                          </div>
                          {memberImpersonateErrors[m.user_id] ? (
                            <p className={`${ccErr} mt-2`} role="alert">
                              {memberImpersonateErrors[m.user_id]}
                            </p>
                          ) : null}
                          {memberResetErrors[m.user_id] ? (
                            <p className={`${ccErr} mt-2`} role="alert">
                              {memberResetErrors[m.user_id]}
                            </p>
                          ) : null}
                          {memberResetInfo[m.user_id] ? (
                            <p className={`${ccHint} mt-2`} role="status">
                              {memberResetInfo[m.user_id]}
                            </p>
                          ) : null}
                          {expandedMemberId === m.user_id ? (
                            <div className="mt-3 border-t border-cc-line pt-3">
                              {pwEventsByUser[m.user_id] === undefined ? (
                                <p className={ccHint}>Loading…</p>
                              ) : pwEventsByUser[m.user_id] === null ? (
                                <p className={ccErr} role="alert">
                                  Could not load password history.
                                </p>
                              ) : (pwEventsByUser[m.user_id] as PasswordEvent[]).length === 0 ? (
                                <p className={ccHint}>No password changes recorded.</p>
                              ) : (
                                <ul className="grid gap-2">
                                  {(pwEventsByUser[m.user_id] as PasswordEvent[]).map((e, i) => (
                                    <li key={i} className="text-[13px]">
                                      <span className="text-cc-muted">
                                        {e.created_at
                                          ? new Date(e.created_at).toLocaleString()
                                          : '—'}
                                      </span>
                                      <span className="mx-2">·</span>
                                      {passwordEventLabel(e)}
                                      {e.ip_address ? (
                                        <span className={`${ccMono} ml-2`}>{e.ip_address}</span>
                                      ) : null}
                                    </li>
                                  ))}
                                </ul>
                              )}
                            </div>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  ) : !membersError ? (
                    <p className={ccHint}>No members found for this org.</p>
                  ) : null}
                </div>
              ) : null}

              {activeTab === 'account-logs' ? (
                <div>
                  <p className={ccHint}>
                    Flag changes and member joins for this org, newest first.
                  </p>
                  {accountLog.length === 0 ? (
                    <p className={ccHint}>No account activity recorded yet.</p>
                  ) : (
                    <ul className="grid gap-3">
                      {accountLog.map((entry, i) => (
                        <li key={i} className="grid gap-0.5 border-b border-cc-line pb-3 last:border-0">
                          <span className="text-[12px] text-cc-muted">{new Date(entry.at).toLocaleString()}</span>
                          <span className="text-sm font-semibold">{entry.summary}</span>
                          <span className={ccHint}>{entry.detail}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ) : null}
            </div>
          </aside>
        </div>
      ) : null}

      {pendingDangerToggle ? (
        <Modal
          title="Confirm danger-zone change"
          onClose={() => setPendingDangerToggle(null)}
        >
          <p>
            {pendingDangerToggle.nextEnabled ? 'Enable' : 'Disable'}{' '}
            <strong>{pendingDangerToggle.def.label}</strong> for{' '}
            <strong>{selectedOrg?.org_name || 'this org'}</strong>? This is logged.
          </p>
          <p className={ccHint}>{pendingDangerToggle.def.description}</p>
          <div className="mt-4 flex justify-end gap-2">
            <button type="button" className={ccGhost} onClick={() => setPendingDangerToggle(null)}>
              Cancel
            </button>
            <button type="button" className={ccBtn} onClick={confirmDangerToggle}>
              Confirm
            </button>
          </div>
        </Modal>
      ) : null}

      {provisionModalOpen ? (
        <Modal title="Provision user" onClose={closeProvisionModal}>
          <p className={ccHint}>
            Creates a login and a new org, named as you choose. The password is
            generated and shown once here — copy it and share it with the
            person alongside their email.
          </p>
          <form className="mt-4 grid gap-3" onSubmit={(e) => void provisionUser(e)}>
            <label className={ccLabel}>
              Email
              <input
                type="email"
                className={ccInput}
                value={pEmail}
                onChange={(e) => setPEmail(e.target.value)}
                required
              />
            </label>
            <label className={ccLabel}>
              First name
              <input
                type="text"
                className={ccInput}
                value={pFirst}
                onChange={(e) => setPFirst(e.target.value)}
                required
              />
            </label>
            <label className={ccLabel}>
              Last name
              <input
                type="text"
                className={ccInput}
                value={pLast}
                onChange={(e) => setPLast(e.target.value)}
                required
              />
            </label>
            <label className={ccLabel}>
              Org name
              <input
                type="text"
                className={ccInput}
                value={pOrgName}
                onChange={(e) => setPOrgName(e.target.value)}
                required
              />
            </label>
            <button type="submit" className={ccBtn} disabled={provisioning}>
              {provisioning ? 'Creating…' : 'Create'}
            </button>
          </form>

          {provisionError ? (
            <p className={`${ccErr} mt-3`} role="alert">
              {provisionError}
            </p>
          ) : null}

          {provisionResult ? (
            <div className="admin-provision-result" role="status">
              <p>
                <strong>{provisionResult.email}</strong>{' '}
                {provisionResult.created ? 'created' : 'added to the existing org'} in{' '}
                <strong>{provisionResult.org_name}</strong>.
              </p>
              <p className="admin-provision-warning">
                This password is shown once — copy it now.
              </p>
              <div className="admin-provision-secret">
                <code>{provisionResult.temporary_password}</code>
                <button type="button" className={ccGhost} onClick={() => void copyPassword()}>
                  {copied ? 'Copied' : 'Copy'}
                </button>
              </div>
            </div>
          ) : null}
        </Modal>
      ) : null}

      {toast ? (
        <div className="fixed bottom-5 left-5 z-50 rounded-md bg-cc-ink px-4 py-2 text-[13px] font-semibold text-white" role="status">
          {toast}
        </div>
      ) : null}
    </CommandCenterPage>
  )
}
