import { NavLink, useLocation } from 'react-router-dom'
import { BrandLogo } from './BrandLogo'
import { PyaiBadge } from './PyaiBadge'
import { UsageMeter } from './UsageMeter'
import { useAudit } from '../context/AuditContext'

const HOME = { to: '/', label: 'Home', end: true, icon: 'home' } as const

const LOOP_NAV = [
  { to: '/feedbacks', label: 'Feedbacks', end: false, icon: 'feedbacks' },
  { to: '/churn-risk', label: 'Churn Risk', end: false, icon: 'churn' },
  { to: '/integrations', label: 'Integrations', end: false, icon: 'integrations' },
  { to: '/training', label: 'Training', end: false, icon: 'training', soon: true },
] as const

type NavIconName = typeof HOME.icon | (typeof LOOP_NAV)[number]['icon'] | 'pulse' | 'neighbourhood'

function NavIcon({ name }: { name: NavIconName }) {
  if (name === 'home') {
    return (
      <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M4.5 11.2 12 5l7.5 6.2V19a1.5 1.5 0 0 1-1.5 1.5h-4.2v-5.2h-3.6V20.5H6A1.5 1.5 0 0 1 4.5 19Z"
        />
      </svg>
    )
  }
  if (name === 'neighbourhood') {
    return (
      <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M3.2 12.2 8 8.2l4.8 4V19.5H3.2Z"
        />
        <path
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M12.2 13 17 9.2 21.8 13v6.5h-9.6"
        />
        <path
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          d="M6.8 19.5v-3.4h2.2v3.4M16.4 19.5v-3.2h2.1v3.2"
        />
      </svg>
    )
  }
  if (name === 'pulse') {
    return (
      <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          d="M4 12h2.8l1.7-4.5 2.6 9 2.2-6.2L15.6 12H20"
        />
      </svg>
    )
  }
  if (name === 'feedbacks') {
    return (
      <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinejoin="round"
          d="M5 7h10a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2H10l-4 3v-3H5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2Z"
        />
      </svg>
    )
  }
  if (name === 'training') {
    return (
      <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M3 10.2 12 5.5 21 10.2 12 14.9Z"
        />
        <path
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M7.2 12.2v4.4c0 1.2 2.1 2.4 4.8 2.4s4.8-1.2 4.8-2.4v-4.4"
        />
        <path
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          d="M21 10.2v6.2"
        />
      </svg>
    )
  }
  if (name === 'integrations') {
    return (
      <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M10 13a5 5 0 0 0 7.07 0l1.41-1.41a5 5 0 0 0-7.07-7.07L10 5.93"
        />
        <path
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M14 11a5 5 0 0 0-7.07 0L5.52 12.41a5 5 0 0 0 7.07 7.07L14 18.07"
        />
      </svg>
    )
  }
  return (
    <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M4 16 9 9l3 4 4-8 4 6"
      />
    </svg>
  )
}

interface SidebarProps {
  open: boolean
  onNavigate: () => void
}

export function Sidebar({ open, onNavigate }: SidebarProps) {
  const { pathname } = useLocation()
  const { flaggedCount } = useAudit()
  const onHome = pathname === '/'
  const pulseOpen = pathname.startsWith('/agents-pulse')

  return (
    <>
      <div
        className={['sidebar-backdrop', open ? 'is-open' : ''].filter(Boolean).join(' ')}
        onClick={onNavigate}
        aria-hidden="true"
      />
      <aside
        className={['sidebar', open ? 'is-open' : ''].filter(Boolean).join(' ')}
        aria-label="Call Loop navigation"
      >
        <div className="sidebar-brand">
          <BrandLogo size="sm" surface="dark" animate={false} />
          <button
            type="button"
            className="sidebar-close"
            aria-label="Close navigation"
            onClick={onNavigate}
          >
            ✕
          </button>
        </div>

        <p className="sidebar-tagline">We close the loop</p>

        <nav className="sidebar-home" aria-label="Home">
          <NavLink
            to={HOME.to}
            end={HOME.end}
            className={({ isActive }) =>
              ['sidebar-link', isActive ? 'is-active' : ''].filter(Boolean).join(' ')
            }
            onClick={onNavigate}
          >
            <NavIcon name={HOME.icon} />
            {HOME.label}
          </NavLink>
          <NavLink
            to="/neighbourhood"
            className={({ isActive }) =>
              ['sidebar-link', isActive ? 'is-active' : '', !isActive && !onHome ? 'is-dim' : '']
                .filter(Boolean)
                .join(' ')
            }
            onClick={onNavigate}
          >
            <NavIcon name="neighbourhood" />
            Neighbourhood
          </NavLink>
        </nav>

        <p className="nav-group">Loop</p>
        <nav className="sidebar-nav" aria-label="Loop">
          <div className={['nav-branch', pulseOpen ? 'is-open' : ''].filter(Boolean).join(' ')}>
            <NavLink
              to="/agents-pulse"
              end
              className={({ isActive }) =>
                ['sidebar-link', isActive ? 'is-active' : '', pulseOpen ? 'is-open' : '']
                  .filter(Boolean)
                  .join(' ')
              }
              onClick={onNavigate}
              aria-expanded={pulseOpen}
            >
              <NavIcon name="pulse" />
              Agent Pulse
            </NavLink>
            {pulseOpen ? (
              <NavLink
                to="/agents-pulse/flagged"
                className={({ isActive }) =>
                  ['sidebar-link', 'is-child', isActive ? 'is-active' : '']
                    .filter(Boolean)
                    .join(' ')
                }
                onClick={onNavigate}
              >
                Flagged for review
                {flaggedCount > 0 ? (
                  <span className="nav-soon">{flaggedCount}</span>
                ) : null}
              </NavLink>
            ) : null}
          </div>
          {LOOP_NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                ['sidebar-link', isActive ? 'is-active' : ''].filter(Boolean).join(' ')
              }
              onClick={onNavigate}
            >
              <NavIcon name={item.icon} />
              {item.label}
              {'soon' in item && item.soon ? <span className="nav-soon">Soon</span> : null}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <PyaiBadge onNavigate={onNavigate} />
          <UsageMeter />
        </div>
      </aside>
    </>
  )
}
