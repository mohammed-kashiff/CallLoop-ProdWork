// AC-56: the exact label a role tag shows, everywhere one is shown
// (Profile page and Command Center's Members tab) — "Team Member" rather
// than the raw "member" the database stores.
export function roleTagLabel(role: string | null | undefined): string {
  if (role === 'owner') return 'Owner'
  if (role === 'manager') return 'Manager'
  if (role === 'member') return 'Team Member'
  return '—'
}
