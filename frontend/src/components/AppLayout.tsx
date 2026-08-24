import { useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { useColorMode } from '../context/ColorMode'
import { BrandLogo } from './BrandLogo'
import { ColorModeToggle } from './ColorModeToggle'
import { KeysPanel } from './KeysPanel'
import { LiveTicker } from './LiveTicker'
import { Sidebar } from './Sidebar'

function themeFromPath(
  pathname: string,
): 'home' | 'agents-pulse' | 'feedbacks' | 'churn-risk' | 'training' {
  if (pathname.startsWith('/feedbacks')) return 'feedbacks'
  if (pathname.startsWith('/churn-risk')) return 'churn-risk'
  if (pathname.startsWith('/training')) return 'training'
  if (pathname.startsWith('/agents-pulse')) return 'agents-pulse'
  return 'home'
}

export function AppLayout() {
  const [navOpen, setNavOpen] = useState(false)
  const { pathname } = useLocation()
  const theme = themeFromPath(pathname)
  const { mode } = useColorMode()
  const { signOut } = useAuth()

  return (
    <div className="app-shell layout-shell" data-theme={theme} data-color-mode={mode}>
      <Sidebar open={navOpen} onNavigate={() => setNavOpen(false)} />

      <div className="content-shell">
        <header className="app-topbar">
          <button
            type="button"
            className="nav-toggle"
            aria-label="Open navigation"
            aria-expanded={navOpen}
            onClick={() => setNavOpen(true)}
          >
            <span />
            <span />
            <span />
          </button>
          <div className="topbar-brand">
            <BrandLogo size="sm" surface={mode === 'dark' ? 'dark' : 'light'} showMark animate={false} />
          </div>
          <p className="topbar-spacer" />
          <LiveTicker />
          <span className="topbar-chip">Rubric v8</span>
          <ColorModeToggle />
          <button type="button" className="ghost-btn" onClick={() => void signOut()}>
            Sign out
          </button>
        </header>

        <main className="main">
          <Outlet />
        </main>
      </div>
      <KeysPanel />
    </div>
  )
}
