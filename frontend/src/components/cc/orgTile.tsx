const TILES = [
  'bg-[#0d9488] text-white',
  'bg-[#0b1f44] text-white',
  'bg-[#0891b2] text-white',
  'bg-[#0f766e] text-white',
] as const

export function ccOrgTileTone(name: string): string {
  const key = name.trim() || '?'
  let h = 0
  for (let i = 0; i < key.length; i++) {
    h = Math.imul(h, 31) + key.charCodeAt(i)
  }
  return TILES[Math.abs(h) % TILES.length]
}

export function CcOrgTile({
  name,
  size = 'sm',
}: {
  name: string
  size?: 'sm' | 'lg'
}) {
  const letter = (name || '?').trim().slice(0, 1).toUpperCase() || '?'
  const dim = size === 'lg' ? 'h-12 w-12 text-lg' : 'h-8 w-8 text-[13px]'
  return (
    <span
      className={`flex shrink-0 items-center justify-center rounded-lg font-semibold ${dim} ${ccOrgTileTone(name)}`}
      aria-hidden="true"
    >
      {letter}
    </span>
  )
}
