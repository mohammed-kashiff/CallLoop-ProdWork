import { Neighbourhood as NeighbourhoodCompare } from '../components/Neighbourhood'

export function Neighbourhood() {
  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Home / Neighbourhood</p>
          <h1>Neighbourhood</h1>
        </div>
      </header>

      <div className="neighbourhood-wrap is-page">
        <NeighbourhoodCompare />
      </div>
    </>
  )
}
