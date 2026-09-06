import {
  Fragment,
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
import { supabase, supabaseConfigured } from '../lib/supabase'
import { useAuth } from '../context/AuthContext'
import { CUSTOMER_ORIGIN, isAdminHost } from '../lib/adminHost'

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
    <div className="cc-modal-backdrop" onClick={onClose}>
      <div
        className="cc-modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="cc-modal-header">
          <h3>{title}</h3>
          <button type="button" className="cc-modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}

/** AC-38: real per-day hits, no mock random numbers — a plain inline SVG
 * line, no charting dependency (matches AC-32's decision not to pull in
 * Tailwind/Chart.js for Command Center). */
function UsageSparkline({ data }: { data: DailyUsagePoint[] }) {
  if (data.length === 0) return <p className="empty-copy">No usage data yet.</p>
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
    <figure className="cc-chart">
      <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label="Daily API hits trend, last 30 days">
        <defs>
          <linearGradient id="cc-chart-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--cc-accent)" stopOpacity="0.35" />
            <stop offset="100%" stopColor="var(--cc-accent)" stopOpacity="0" />
          </linearGradient>
        </defs>
        {gridLines.map((g) => (
          <line
            key={g}
            x1={padX}
            x2={w - padX}
            y1={padTop + g * (h - padBottom - padTop)}
            y2={padTop + g * (h - padBottom - padTop)}
            stroke="var(--cc-line)"
            strokeWidth="1"
          />
        ))}
        <polygon points={areaPoints} fill="url(#cc-chart-fill)" stroke="none" />
        <polyline points={linePoints} fill="none" stroke="var(--cc-accent)" strokeWidth="2.5" />
        {data.map((d, i) =>
          i % labelEvery === 0 ? (
            <text
              key={d.date}
              x={xAt(i)}
              y={h - 8}
              fontSize="10"
              textAnchor="middle"
              fill="var(--cc-ink-muted)"
            >
              {d.date.slice(5)}
            </text>
          ) : null,
        )}
      </svg>
      <figcaption className="admin-provision-hint">
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
  const [activeTab, setActiveTab] = useState<'overview' | 'flags' | 'rubric' | 'members'>(
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
    setBusy(true)
    try {
      const [usageRes, detailRes, rubricRes, dailyRes] = await Promise.all([
        apiFetch(`/api/admin/usage?org_id=${encodeURIComponent(row.org_id)}`),
        apiFetch(`/api/admin/orgs/${encodeURIComponent(row.org_id)}/detail`),
        apiFetch(`/api/admin/orgs/${encodeURIComponent(row.org_id)}/rubric`),
        apiFetch(`/api/admin/usage/daily?org_id=${encodeURIComponent(row.org_id)}&days=30`),
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
    if (isAdminHost()) {
      return (
        <>
          <header className="page-bar">
            <div>
              <p className="crumb">Platform</p>
              <h1>Admin</h1>
            </div>
          </header>
          <p className="admin-provision-hint">
            This console is limited to platform admins.
          </p>
        </>
      )
    }
    return <Navigate to="/" replace />
  }

  const pyai = usage?.usage?.by_provider?.pyai
  const claude = usage?.usage?.by_provider?.anthropic
  const flagsByRisk = RISK_ORDER.map((risk) => ({
    risk,
    defs: flagDefs.filter((d) => d.risk === risk),
  })).filter((g) => g.defs.length > 0)

  return (
    <div className="cc-shell">
      <header className="page-bar">
        <div>
          <p className="crumb">Platform</p>
          <h1>Command Center</h1>
        </div>
      </header>

      {/* Not part of AC-37/38/39/40's scope — kept as-is, unrelocated. */}
      <section className="admin-activity">
        <h2>Activity</h2>
        <p className="admin-provision-hint">
          Uploads, audits, and flag changes for one org in a date range.
          Retranscribes show as a new audit on that call. Not application logs.
        </p>
        <form className="admin-provision-form" onSubmit={(e) => void loadActivity(e)}>
          <label>
            Org id or short id
            <input
              type="text"
              value={actOrg}
              onChange={(e) => setActOrg(e.target.value)}
              placeholder="UUID or 100001"
              required
            />
          </label>
          <label>
            From
            <input
              type="date"
              value={actSince}
              onChange={(e) => setActSince(e.target.value)}
              required
            />
          </label>
          <label>
            To
            <input
              type="date"
              value={actUntil}
              onChange={(e) => setActUntil(e.target.value)}
              required
            />
          </label>
          <button type="submit" className="start-btn" disabled={actBusy}>
            {actBusy ? 'Loading…' : 'Load'}
          </button>
        </form>
        {actError ? (
          <p className="upload-error" role="alert">
            {actError}
          </p>
        ) : null}
        {activity ? (
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Type</th>
                  <th>Who</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {activity.events.map((ev, i) => (
                  <tr key={`${ev.kind}-${ev.at}-${ev.call_id ?? ev.feature_key ?? i}`}>
                    <td>{ev.at ? new Date(ev.at).toLocaleString() : '—'}</td>
                    <td>{activityKind(ev.kind)}</td>
                    <td>{ev.actor || '—'}</td>
                    <td>{activityLabel(ev)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {activity.events.length === 0 ? (
              <p className="empty-copy">No activity in that window.</p>
            ) : null}
            {activity.truncated ? (
              <p className="admin-provision-hint">Showing the most recent rows in this range.</p>
            ) : null}
          </div>
        ) : null}
      </section>

      {/* AC-37/AC-39/AC-40: top bar — real org search (⌘K focuses it) and
          the Provision-user action, now a modal trigger instead of an
          always-open inline form. */}
      <div className="cc-topbar">
        <label className="cc-search">
          <span className="sr-only">Search directory</span>
          <input
            ref={searchInputRef}
            type="search"
            value={orgQuery}
            onChange={(e) => setOrgQuery(e.target.value)}
            placeholder="Search orgs — email, name, org id, short id (⌘K)"
          />
        </label>
        <button type="button" className="start-btn" onClick={() => setProvisionModalOpen(true)}>
          Provision user
        </button>
      </div>

      {orgSearchError ? (
        <p className="upload-error" role="alert">
          {orgSearchError}
        </p>
      ) : null}

      {/* AC-37: the directory table — one row per org (AC-33), never per
          member. Row click opens the inspector; the table itself never
          unmounts, so its scroll position survives the drawer opening
          and closing. */}
      <div className="cc-directory-card">
        <div className="cc-directory-card-header">
          <h2>Organizations</h2>
          <span className="cc-directory-count">
            {orgRows.length} {orgRows.length === 1 ? 'organization' : 'organizations'}
          </span>
        </div>
        <div className="admin-table-wrap">
          <table className="admin-table cc-table">
            <thead>
              <tr>
                <th>Org</th>
                <th>Members</th>
                <th>Short IDs</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {orgRows.map((row) => (
                <tr
                  key={row.org_id}
                  className={`cc-row-clickable${selectedOrg?.org_id === row.org_id ? ' is-selected' : ''}`}
                  onClick={() => void openOrg(row)}
                >
                  <td>
                    <div className="cc-org-cell">
                      <span className="cc-org-avatar" aria-hidden="true">
                        {(row.org_name || '?').slice(0, 1).toUpperCase()}
                      </span>
                      <span>
                        <span className="admin-org">{row.org_name || '—'}</span>
                        <span className="admin-id">{row.org_id}</span>
                      </span>
                    </div>
                  </td>
                  <td>{row.member_count}</td>
                  <td>{row.short_ids.length > 0 ? row.short_ids.join(', ') : '—'}</td>
                  <td>{row.created_at ? new Date(row.created_at).toLocaleDateString() : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {orgRows.length === 0 ? <p className="empty-copy">No matching orgs.</p> : null}
        </div>
      </div>

      {/* AC-37: slide-over inspector. Backdrop click or Escape closes it. */}
      {selectedOrg ? (
        <div className="cc-drawer-backdrop" onClick={closeDrawer}>
          <aside
            className="cc-drawer"
            role="dialog"
            aria-modal="true"
            aria-label={selectedOrg.org_name || 'Organization'}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="cc-drawer-header">
              <div>
                <h2>{selectedOrg.org_name || 'Organization'}</h2>
                <p className="admin-id">{selectedOrg.org_id}</p>
              </div>
              <button type="button" className="cc-modal-close" onClick={closeDrawer} aria-label="Close">
                ×
              </button>
            </div>

            <nav className="cc-tabs" aria-label="Org detail tabs">
              {(['overview', 'flags', 'rubric', 'members'] as const).map((tab) => (
                <button
                  key={tab}
                  type="button"
                  className={`cc-tab${activeTab === tab ? ' is-active' : ''}`}
                  onClick={() => setActiveTab(tab)}
                >
                  {tab === 'overview' ? 'Overview' : tab === 'flags' ? 'Flags' : tab === 'rubric' ? 'Rubric' : 'Members'}
                </button>
              ))}
            </nav>

            {detailError ? (
              <p className="upload-error" role="alert">
                {detailError}
              </p>
            ) : null}
            {busy ? <p className="empty-copy">Loading…</p> : null}

            <div className="cc-tab-panel">
              {activeTab === 'overview' ? (
                <div className="admin-card">
                  <dl className="admin-stats">
                    <div>
                      <dt>Members</dt>
                      <dd>{selectedOrg.member_count}</dd>
                    </div>
                    <div>
                      <dt>Created</dt>
                      <dd>
                        {selectedOrg.created_at
                          ? new Date(selectedOrg.created_at).toLocaleDateString()
                          : '—'}
                      </dd>
                    </div>
                  </dl>

                  {usage || orgDetail ? (
                    <div className="cc-stat-grid">
                      {usage ? (
                        <>
                          <div className="cc-stat-card cc-stat-accent">
                            <p className="cc-stat-label">PyAI calls</p>
                            <p className="cc-stat-value">{pyai?.hits ?? 0}</p>
                          </div>
                          <div className="cc-stat-card cc-stat-good">
                            <p className="cc-stat-label">Anthropic calls</p>
                            <p className="cc-stat-value">{claude?.hits ?? 0}</p>
                          </div>
                          <div className="cc-stat-card cc-stat-warn">
                            <p className="cc-stat-label">Est. spend</p>
                            <p className="cc-stat-value">{fmtUsd(usage.cost.total_usd)}</p>
                          </div>
                        </>
                      ) : null}
                      {orgDetail ? (
                        <div className="cc-stat-card">
                          <p className="cc-stat-label">Data stored</p>
                          <p className="cc-stat-value">{formatBytes(orgDetail.total_data_size_bytes)}</p>
                        </div>
                      ) : null}
                    </div>
                  ) : null}

                  {orgDetail ? (
                    <dl className="admin-stats">
                      <div>
                        <dt>Total calls</dt>
                        <dd>{orgDetail.total_calls}</dd>
                      </div>
                      <div>
                        <dt>Audited</dt>
                        <dd>{orgDetail.audited_count}</dd>
                      </div>
                      <div>
                        <dt>PyAI polls</dt>
                        <dd>{pyai?.polls ?? 0}</dd>
                      </div>
                    </dl>
                  ) : null}

                  <h3>Usage trend — last 30 days</h3>
                  <UsageSparkline data={daily} />
                  <p className="admin-provision-hint">
                    Per-call detail moved to <Link to="/call-logs">Call logs</Link>.
                  </p>
                </div>
              ) : null}

              {activeTab === 'flags' ? (
                <div className="admin-card">
                  {flagsByRisk.length === 0 ? (
                    <p className="empty-copy">Loading flag definitions…</p>
                  ) : (
                    flagsByRisk.map(({ risk, defs }) => (
                      <div key={risk} className={`cc-flag-group cc-risk-${risk}`}>
                        <h4 className="cc-flag-group-title">{RISK_LABEL[risk]}</h4>
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
                                  <p className="admin-provision-hint">{def.description}</p>
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
                <div className="admin-card">
                  {rubricError ? (
                    <p className="upload-error" role="alert">
                      {rubricError}
                    </p>
                  ) : null}
                  {rubric ? (
                    <>
                      <div className="cc-rubric-hero">
                        <div>
                          <p className="cc-rubric-hero-version">
                            {rubric.source === 'custom'
                              ? `Custom — version ${rubric.version}`
                              : 'Not yet customized'}
                          </p>
                          <p className="cc-rubric-hero-updated">
                            {rubric.updated_at
                              ? `Updated ${new Date(rubric.updated_at).toLocaleString()}`
                              : 'Showing default weights'}
                          </p>
                        </div>
                        <div
                          className={`cc-rubric-hero-total${
                            rubricTotal === 100
                              ? ' is-good'
                              : rubricTotal > 100
                                ? ' is-danger'
                                : ' is-warn'
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
                        className="start-btn"
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
                <div className="admin-card">
                  <p className="admin-provision-hint">
                    Every real member of this org — each has their own "Log
                    in as," never ambiguous about who's being impersonated.
                  </p>
                  {membersError ? (
                    <p className="upload-error" role="alert">
                      {membersError}
                    </p>
                  ) : null}
                  {orgMembers.length > 0 ? (
                    <div className="admin-table-wrap">
                      <table className="admin-table">
                        <thead>
                          <tr>
                            <th>Name</th>
                            <th>Email</th>
                            <th>Role</th>
                            <th>Short ID</th>
                            <th></th>
                          </tr>
                        </thead>
                        <tbody>
                          {orgMembers.map((m) => (
                            <Fragment key={m.user_id}>
                              <tr>
                                <td>{displayName(m)}</td>
                                <td>{m.email || '—'}</td>
                                <td>{m.role || '—'}</td>
                                <td>{m.short_id ?? '—'}</td>
                                <td className="cc-member-actions">
                                  <button
                                    type="button"
                                    className="ghost-btn"
                                    disabled={impersonatingMemberId === m.user_id || !m.email}
                                    onClick={() => void logInAsMember(m)}
                                  >
                                    {impersonatingMemberId === m.user_id ? 'Starting…' : 'Log in as'}
                                  </button>
                                  <button
                                    type="button"
                                    className="ghost-btn"
                                    disabled={
                                      resettingMemberId === m.user_id ||
                                      !m.email ||
                                      !supabaseConfigured
                                    }
                                    onClick={() => void sendResetEmailForMember(m)}
                                  >
                                    {resettingMemberId === m.user_id ? 'Sending…' : 'Send reset email'}
                                  </button>
                                  <button
                                    type="button"
                                    className="ghost-btn"
                                    onClick={() => void toggleMemberHistory(m)}
                                  >
                                    {expandedMemberId === m.user_id ? 'Hide history' : 'History'}
                                  </button>
                                  {memberImpersonateErrors[m.user_id] ? (
                                    <p className="upload-error" role="alert">
                                      {memberImpersonateErrors[m.user_id]}
                                    </p>
                                  ) : null}
                                  {memberResetErrors[m.user_id] ? (
                                    <p className="upload-error" role="alert">
                                      {memberResetErrors[m.user_id]}
                                    </p>
                                  ) : null}
                                  {memberResetInfo[m.user_id] ? (
                                    <p className="auth-info" role="status">
                                      {memberResetInfo[m.user_id]}
                                    </p>
                                  ) : null}
                                </td>
                              </tr>
                              {expandedMemberId === m.user_id ? (
                                <tr key={`${m.user_id}-history`}>
                                  <td colSpan={5}>
                                    {pwEventsByUser[m.user_id] === undefined ? (
                                      <p className="empty-copy">Loading…</p>
                                    ) : pwEventsByUser[m.user_id] === null ? (
                                      <p className="upload-error" role="alert">
                                        Could not load password history.
                                      </p>
                                    ) : (pwEventsByUser[m.user_id] as PasswordEvent[]).length === 0 ? (
                                      <p className="empty-copy">No password changes recorded.</p>
                                    ) : (
                                      <table className="admin-table">
                                        <thead>
                                          <tr>
                                            <th>When</th>
                                            <th>Event</th>
                                            <th>IP</th>
                                          </tr>
                                        </thead>
                                        <tbody>
                                          {(pwEventsByUser[m.user_id] as PasswordEvent[]).map((e, i) => (
                                            <tr key={i}>
                                              <td>
                                                {e.created_at
                                                  ? new Date(e.created_at).toLocaleString()
                                                  : '—'}
                                              </td>
                                              <td>{passwordEventLabel(e)}</td>
                                              <td>{e.ip_address || '—'}</td>
                                            </tr>
                                          ))}
                                        </tbody>
                                      </table>
                                    )}
                                  </td>
                                </tr>
                              ) : null}
                            </Fragment>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : !membersError ? (
                    <p className="empty-copy">No members found for this org.</p>
                  ) : null}
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
          <p className="admin-provision-hint">{pendingDangerToggle.def.description}</p>
          <div className="cc-modal-actions">
            <button type="button" className="ghost-btn" onClick={() => setPendingDangerToggle(null)}>
              Cancel
            </button>
            <button type="button" className="start-btn" onClick={confirmDangerToggle}>
              Confirm
            </button>
          </div>
        </Modal>
      ) : null}

      {provisionModalOpen ? (
        <Modal title="Provision user" onClose={closeProvisionModal}>
          <p className="admin-provision-hint">
            Creates a login and a new org, named as you choose. The password is
            generated and shown once here — copy it and share it with the
            person alongside their email.
          </p>
          <form className="admin-provision-form" onSubmit={(e) => void provisionUser(e)}>
            <label>
              Email
              <input
                type="email"
                value={pEmail}
                onChange={(e) => setPEmail(e.target.value)}
                required
              />
            </label>
            <label>
              First name
              <input
                type="text"
                value={pFirst}
                onChange={(e) => setPFirst(e.target.value)}
                required
              />
            </label>
            <label>
              Last name
              <input
                type="text"
                value={pLast}
                onChange={(e) => setPLast(e.target.value)}
                required
              />
            </label>
            <label>
              Org name
              <input
                type="text"
                value={pOrgName}
                onChange={(e) => setPOrgName(e.target.value)}
                required
              />
            </label>
            <button type="submit" className="start-btn" disabled={provisioning}>
              {provisioning ? 'Creating…' : 'Create'}
            </button>
          </form>

          {provisionError ? (
            <p className="upload-error" role="alert">
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
                <button type="button" className="ghost-btn" onClick={() => void copyPassword()}>
                  {copied ? 'Copied' : 'Copy'}
                </button>
              </div>
            </div>
          ) : null}
        </Modal>
      ) : null}
    </div>
  )
}
