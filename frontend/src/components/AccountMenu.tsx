import { useEffect, useId, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

function displayName(
  firstName: string | null,
  lastName: string | null,
  email: string | null,
): string {
  const name = [firstName, lastName].filter(Boolean).join(' ').trim()
  return name || email || 'Account'
}

export function AccountMenu() {
  const { pathname } = useLocation()
  const { email, firstName, lastName, signOut } = useAuth()
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const menuId = useId()
  const name = displayName(firstName, lastName, email)
  const showEmail = Boolean(email && name !== email)
  const onProfile = pathname.startsWith('/profile')

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false)
        triggerRef.current?.focus()
      }
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const close = () => setOpen(false)

  return (
    <div className={['account-menu', open ? 'is-open' : ''].join(' ')} ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className={['mode-toggle', open || onProfile ? 'is-current' : '']
          .filter(Boolean)
          .join(' ')}
        aria-label="Account menu"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        title="Account"
        onClick={() => setOpen((v) => !v)}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <circle
            cx="12"
            cy="8.2"
            r="3.1"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
          />
          <path
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            d="M5.6 19.2c.7-3.2 3.2-5 6.4-5s5.7 1.8 6.4 5"
          />
        </svg>
      </button>

      {open ? (
        <div className="account-menu-panel">
          <div className="account-menu-header">
            <p className="account-menu-name">{name}</p>
            {showEmail ? <p className="account-menu-email">{email}</p> : null}
          </div>
          <div id={menuId} role="menu" aria-label="Account">
            <Link
              role="menuitem"
              to="/profile"
              className="account-menu-item"
              onClick={close}
            >
              Profile
            </Link>
            <button
              type="button"
              role="menuitem"
              className="account-menu-item"
              onClick={() => {
                close()
                void signOut()
              }}
            >
              Sign out
            </button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
