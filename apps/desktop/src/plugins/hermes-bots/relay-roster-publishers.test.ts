/**
 * Roster publisher identity + backend install identity — the frontend half.
 *
 * Several Desktops (and several windows of one Desktop) hold sockets to the
 * same gateway and each pushes its own view of the peer set, while the gateway
 * keeps ONE roster. Two views that knew different peers erased each other every
 * tick: the SH gateway flapped n=17 ↔ n=15 once a minute and the rows only the
 * larger view knew vanished for 8–16 s at a time.
 *
 * The client's half of the contract pinned here:
 *
 *  - every push (and the sole-connection clear) names its publisher as
 *    `<install_id>:<window>`, so a gateway can keep one shard per window and
 *    serve the union instead of last-writer-wins;
 *  - every pushed row carries its connection's backend `install_id`, which is
 *    what lets the gateway recognise "the same machine over two routes" and
 *    merge it into one bot (tools/bot_relay.py owns that merge).
 *
 * A pre-contract gateway ignores `publisher` and `install_id`; the same
 * alternation then reproduces the flap, so the suite cannot pass by dropping
 * the stamps again.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ProfileRoute } from './types'

const INSTALL_KEY = 'hermes.desktop.botRelay.install.v1'
const GATEWAY = 'sh'
const STUDIO_INSTALL = '0096c20b30dc42f3'

/** The 17-row view; the 15-row view knows only the Macs. */
const STUDIO_PROFILES = Array.from({ length: 14 }, (_, index) => `studio-bot-${index + 1}`)

const PROFILES: Record<string, string[]> = {
  [GATEWAY]: ['hermes'],
  'mac-studio': STUDIO_PROFILES,
  'mac-mini': ['mini-hermes'],
  leiihome: ['hermes'],
  luxetjim: ['hermes']
}

const LABELS: Record<string, string> = {
  [GATEWAY]: 'SH 云服务器',
  'mac-studio': 'Mac Studio',
  'mac-mini': 'Mac mini',
  leiihome: 'LEIIHOME',
  luxetjim: 'LuxetJime'
}

const PUBLISHER_RE = /^[A-Za-z0-9_-]{8,80}:win-[0-9a-f]{4}$/

const { hostMock, state, UnboundedCache } = vi.hoisted(() => {
  const state = {
    /** 'publisher' = the contract a gateway adopts; 'replace' = pre-contract. */
    mode: 'publisher' as 'publisher' | 'replace',
    buckets: new Map<string, Array<Record<string, unknown>>>(),
    replaced: [] as Array<Record<string, unknown>>,
    routes: [] as ProfileRoute[],
    /** Backend install id per connection, as `/api/status` would report it. */
    installs: {} as Record<string, string>,
    syncs: [] as Array<{ connectionId: string; params: Record<string, unknown> }>
  }

  return {
    state,
    UnboundedCache: class extends Map {
      constructor(_max: number) {
        super()
      }
    },
    hostMock: {
      onEvent: vi.fn(() => vi.fn()),
      pluginDecisions: { get: () => ({}) },
      profileRoutes: vi.fn(async () => state.routes),
      connections: vi.fn(async () =>
        Object.keys(LABELS).map(id => ({ id, label: LABELS[id], primary: id === GATEWAY }))
      ),
      requestProfile: vi.fn(async (target: ProfileRoute, method: string, params: Record<string, unknown>) => {
        if (method === 'profiles.list') {
          const names = PROFILES[target.connectionId] ?? []
          const installId = state.installs[target.connectionId]

          return {
            profiles: names.map(name => ({ name })),
            ...(installId ? { install_id: installId } : {})
          }
        }

        if (method === 'bot_relay.roster.sync') {
          state.syncs.push({ connectionId: target.connectionId, params: structuredClone(params ?? {}) })

          // Only the SH gateway holds the shared roster in this incident; the
          // other gateways accept their own copy and are not modelled.
          if (target.connectionId === GATEWAY) {
            const rows = (Array.isArray(params?.agents) ? params.agents : []) as Array<Record<string, unknown>>
            const publisher = String(params?.publisher ?? '')

            if (state.mode === 'publisher' && publisher) {
              state.buckets.set(publisher, rows)
            } else {
              state.replaced = rows
            }
          }

          return { count: (params?.agents as unknown[] | undefined)?.length ?? 0 }
        }

        return {}
      }),
      retainProfileSocket: vi.fn(() => vi.fn())
    }
  }
})

vi.mock('@hermes/plugin-sdk', async () => {
  const { atom } = await import('nanostores')

  return { atom, host: hostMock, LruCache: UnboundedCache }
})

vi.mock('./data', () => ({
  botHandle: (name: string) => (name === 'default' ? 'hermes' : name),
  clearBotAttention: vi.fn(),
  noteBotAttention: vi.fn()
}))

const route = (connectionId: string, profile = 'default'): ProfileRoute => ({
  connectionId,
  mode: 'remote',
  profile,
  targetProfile: profile
})

const HEAVY_ROUTES = ['sh', 'mac-studio', 'mac-mini', 'leiihome', 'luxetjim'].map(id => route(id))
const LIGHT_ROUTES = ['sh', 'mac-studio', 'mac-mini'].map(id => route(id))

/** What a publisher-scoped gateway serves: the union of every window's shard. */
function gatewayRows(): Array<Record<string, unknown>> {
  if (state.mode === 'replace') {
    return state.replaced
  }

  const byKey = new Map<string, Record<string, unknown>>()

  for (const rows of state.buckets.values()) {
    for (const row of rows) {
      byKey.set(`${String(row.connection_id)}/${String(row.profile)}`, row)
    }
  }

  return [...byKey.values()]
}

const windowsRows = () =>
  gatewayRows().filter(row => row.connection_id === 'leiihome' || row.connection_id === 'luxetjim')

/** One Desktop instance: its own relay module, hence its own window id. */
interface Desktop {
  heavy: boolean
  relay: { startBotRelay: () => void; stopBotRelay: () => void }
}

async function openDesktop(routes: ProfileRoute[]): Promise<Desktop> {
  vi.resetModules()
  const relay = await import('./relay')

  state.routes = routes
  relay.startBotRelay()
  await vi.advanceTimersByTimeAsync(0)
  relay.stopBotRelay()

  return { heavy: routes.length > LIGHT_ROUTES.length, relay }
}

/** One 60 s tick of that Desktop — its own phase, never concurrent. */
async function tick(desktop: Desktop) {
  state.routes = desktop.heavy ? HEAVY_ROUTES : LIGHT_ROUTES
  desktop.relay.startBotRelay()
  await vi.advanceTimersByTimeAsync(0)
  desktop.relay.stopBotRelay()
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  window.localStorage.clear()
  state.mode = 'publisher'
  state.buckets.clear()
  state.replaced = []
  state.routes = []
  state.installs = {}
  state.syncs = []
})

afterEach(() => {
  vi.useRealTimers()
})

describe('two Desktops publishing 17-row and 15-row views', () => {
  it('keeps the two Windows rows across alternating publishes (publisher-scoped gateway)', async () => {
    state.installs[GATEWAY] = STUDIO_INSTALL

    const heavy = await openDesktop(HEAVY_ROUTES)

    expect(gatewayRows()).toHaveLength(17)
    expect(windowsRows().map(row => row.connection_id).sort()).toEqual(['leiihome', 'luxetjim'])

    const light = await openDesktop(LIGHT_ROUTES)

    // The 15-row view lands second — the write that used to wipe the Windows rows.
    expect(windowsRows()).toHaveLength(2)

    for (let round = 0; round < 3; round += 1) {
      await tick(heavy)
      expect(gatewayRows()).toHaveLength(17)
      expect(windowsRows().map(row => row.connection_id).sort()).toEqual(['leiihome', 'luxetjim'])

      await tick(light)
      expect(gatewayRows()).toHaveLength(17)
      expect(windowsRows().map(row => row.connection_id).sort()).toEqual(['leiihome', 'luxetjim'])
    }
  })

  it('reproduces the flap on a pre-contract gateway (the bug this fix names)', async () => {
    state.mode = 'replace'

    const heavy = await openDesktop(HEAVY_ROUTES)
    expect(gatewayRows()).toHaveLength(17)

    const light = await openDesktop(LIGHT_ROUTES)

    // Whole-file replace: the smaller view's push is the n=15 sample.
    expect(gatewayRows()).toHaveLength(15)
    expect(windowsRows()).toHaveLength(0)

    await tick(heavy)
    expect(gatewayRows()).toHaveLength(17)
  })

  it('stamps <install_id>:<window> on every push and on the sole-connection clear', async () => {
    state.installs[GATEWAY] = STUDIO_INSTALL

    const heavy = await openDesktop(HEAVY_ROUTES)
    const light = await openDesktop(LIGHT_ROUTES)

    await tick(heavy)

    const toGateway = state.syncs.filter(call => call.connectionId === GATEWAY)
    const publishers = toGateway.map(call => String(call.params.publisher))

    // One window id per renderer run, the install it runs on in front of it.
    expect(publishers).toHaveLength(3)

    for (const publisher of publishers) {
      expect(publisher).toMatch(PUBLISHER_RE)
      expect(publisher.startsWith(`${STUDIO_INSTALL}:`)).toBe(true)
    }

    // Same window, same publisher on its next tick.
    expect(publishers[0]).toBe(publishers[2])
    expect(publishers[0]).not.toBe(publishers[1])

    // The clear only forgets ITS OWN shard, so it must carry the same publisher.
    const sole = await openDesktop([route(GATEWAY)])
    const clear = state.syncs.filter(call => call.connectionId === GATEWAY).at(-1)

    expect(clear?.params.agents).toEqual([])
    expect(String(clear?.params.publisher)).toMatch(PUBLISHER_RE)
    expect(gatewayRows()).toHaveLength(17) // the other windows' views survived it
    expect(sole.heavy).toBe(false)
  })

  it('falls back to the persisted install segment when no backend reports one', async () => {
    window.localStorage.setItem(INSTALL_KEY, 'desktop-0123456789abcdef')

    await openDesktop(HEAVY_ROUTES)

    const publisher = String(
      state.syncs.filter(call => call.connectionId === GATEWAY).at(-1)?.params.publisher
    )

    expect(publisher).toMatch(PUBLISHER_RE)
    expect(publisher.startsWith('desktop-0123456789abcdef:')).toBe(true)
  })
})

describe('pushed rows carry the backend install identity', () => {
  it('stamps the same install_id on both routes of one machine', async () => {
    // Mac Studio is reachable as its LAN address and through the SH tunnel; both
    // connections answer /api/status with the SAME install id.
    state.installs[GATEWAY] = STUDIO_INSTALL
    state.installs['mac-studio-lan'] = STUDIO_INSTALL
    state.installs['mac-studio-via-sh'] = STUDIO_INSTALL
    PROFILES['mac-studio-lan'] = ['default']
    PROFILES['mac-studio-via-sh'] = ['default']

    try {
      await openDesktop([route(GATEWAY, 'ops'), route('mac-studio-lan', 'ops'), route('mac-studio-via-sh', 'ops')])

      const pushed = state.syncs.filter(call => call.connectionId === GATEWAY).at(-1)
      const rows = pushed?.params.agents as Array<Record<string, unknown>>

      const studioRows = rows.filter(row => String(row.connection_id).startsWith('mac-studio'))

      expect(studioRows).toHaveLength(2) // two routes are still two rows on the wire ...
      expect(new Set(studioRows.map(row => row.install_id))).toEqual(new Set([STUDIO_INSTALL])) // ... one install
    } finally {
      delete PROFILES['mac-studio-lan']
      delete PROFILES['mac-studio-via-sh']
    }
  })

  it('omits install_id for a backend that reports none (no collapse, as before)', async () => {
    const installs = state.installs

    state.installs = {} // older backend: /api/status carries no install_id

    try {
      // mac-mini has no install id; SH does, so the push target is not the point here.
      state.installs = { [GATEWAY]: STUDIO_INSTALL }
      await openDesktop([route(GATEWAY, 'ops'), route('mac-mini', 'ops')])

      const rows = state.syncs.filter(call => call.connectionId === GATEWAY).at(-1)?.params
        .agents as Array<Record<string, unknown>>

      const mini = rows.find(row => row.connection_id === 'mac-mini')

      expect(mini).toBeDefined()
      expect('install_id' in (mini as Record<string, unknown>)).toBe(false)
    } finally {
      state.installs = installs
    }
  })

  it('ignores a malformed install_id instead of shipping it to the gateway', async () => {
    state.installs = { [GATEWAY]: STUDIO_INSTALL, 'mac-mini': 'not a valid id!' }

    try {
      await openDesktop([route(GATEWAY, 'ops'), route('mac-mini', 'ops')])

      const rows = state.syncs.filter(call => call.connectionId === GATEWAY).at(-1)?.params
        .agents as Array<Record<string, unknown>>

      const mini = rows.find(row => row.connection_id === 'mac-mini')

      expect(mini).toBeDefined()
      expect('install_id' in (mini as Record<string, unknown>)).toBe(false)
    } finally {
      state.installs = {}
    }
  })
})
