import { useState } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import { useColorMode } from '../context/ColorMode'
import { AccountMenu } from './AccountMenu'
import { BrandLogo } from './BrandLogo'
import { ColorModeToggle } from './ColorModeToggle'
import { ImpersonationBanner } from './ImpersonationBanner'
import { IntercomWidget } from './IntercomWidget'
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
    <div
      className={
        adminHost
          ? 'layout-shell layout-shell--admin min-h-screen bg-cc-paper text-cc-ink'
          : 'app-shell layout-shell'
      }
      data-theme={theme}
      data-color-mode={mode}
    >
      {adminHost ? null : <ImpersonationBanner />}
      <Sidebar open={navOpen} onNavigate={() => setNavOpen(false)} />

      <div className={adminHost ? 'flex min-h-screen min-w-0 flex-col bg-cc-paper' : 'content-shell'}>
        <header
          className={
            adminHost
              ? 'flex items-center gap-3 border-b border-cc-line border-t-4 border-t-cc-accent bg-cc-paper px-4 py-3'
              : 'app-topbar'
          }
        >
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

        <main className={adminHost ? 'min-w-0 flex-1 px-6 py-8' : 'main'}>
          <Outlet />
        </main>
      </div>
      {adminHost ? null : <KeysPanel />}
      {adminHost ? null : <IntercomWidget />}
    </div>
  )
}
