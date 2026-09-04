import { useState } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import { useColorMode } from '../context/ColorMode'
import { AccountMenu } from './AccountMenu'
import { BrandLogo } from './BrandLogo'
import { ColorModeToggle } from './ColorModeToggle'
import { ImpersonationBanner } from './ImpersonationBanner'
import { KeysPanel } from './KeysPanel'
import { LiveTicker } from './LiveTicker'
import { Sidebar } from './Sidebar'
import { appHomePath, isAdminHost } from '../lib/adminHost'

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
  const home = appHomePath()
  const adminHost = isAdminHost()

  return (
    <div className="app-shell layout-shell" data-theme={theme} data-color-mode={mode}>
      {adminHost ? null : <ImpersonationBanner />}
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
          <Link to={home} className="topbar-brand" aria-label={adminHost ? 'Go to admin' : 'Go to home'}>
            <BrandLogo size="sm" surface={mode === 'dark' ? 'dark' : 'light'} showMark animate={false} />
          </Link>
          <p className="topbar-spacer" />
          {adminHost ? null : <LiveTicker />}
          {adminHost ? null : <span className="topbar-chip">Rubric v8</span>}
          <ColorModeToggle />
          <AccountMenu />
        </header>

        <main className="main">
          <Outlet />
        </main>
      </div>
      {adminHost ? null : <KeysPanel />}
    </div>
  )
}
