/**
 * Who is signed in.
 *
 * Staff sign in with a password. Administrator and System Admin get an MFA
 * challenge instead of tokens, and sign-in is not finished until the code is
 * verified — the challenge is held in component state only, never persisted,
 * because a half-finished sign-in that survives a page reload is a
 * half-finished sign-in somebody can walk up to.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import { auth as authApi } from '@/api/endpoints'
import { ApiError, SessionExpired, tokens } from '@/api/client'
import { isMfaChallenge } from '@/api/types'
import type { UserProfile } from '@/api/types'

/** Section 4/M3 gates the whole console on this permission. */
export const CONSOLE_PERMISSION = 'crowd:view_detail'

type Status = 'checking' | 'anonymous' | 'mfa' | 'authenticated'

interface AuthState {
  status: Status
  user: UserProfile | null
  error: string | null
  /** True when the account is valid but the role cannot open this console. */
  forbidden: boolean
  signIn: (phone: string, password: string) => Promise<void>
  /** Development only — see `auth.devLogin`. Absent from a production build. */
  devSignIn: (phone: string) => Promise<void>
  verifyMfa: (code: string) => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>('checking')
  const [user, setUser] = useState<UserProfile | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [mfaToken, setMfaToken] = useState<string | null>(null)

  const adopt = useCallback((profile: UserProfile) => {
    setUser(profile)
    setMfaToken(null)
    setError(null)
    setStatus('authenticated')
  }, [])

  // Restore a session across a page reload. An operator who refreshes during a
  // surge should not have to find their password.
  useEffect(() => {
    let cancelled = false
    if (!tokens.access() && !tokens.refresh()) {
      setStatus('anonymous')
      return
    }
    authApi
      .me()
      .then((profile) => {
        if (!cancelled) adopt(profile)
      })
      .catch(() => {
        if (cancelled) return
        tokens.clear()
        setStatus('anonymous')
      })
    return () => {
      cancelled = true
    }
  }, [adopt])

  const signIn = useCallback(async (phone: string, password: string) => {
    setError(null)
    try {
      const result = await authApi.login(phone, password)
      if (isMfaChallenge(result)) {
        setMfaToken(result.mfa_token)
        setStatus('mfa')
        return
      }
      tokens.set(result)
      adopt(result.user)
    } catch (exc) {
      setError(describe(exc))
      throw exc
    }
  }, [adopt])

  const devSignIn = useCallback(
    async (phone: string) => {
      setError(null)
      try {
        const result = await authApi.devLogin(phone)
        tokens.set(result)
        adopt(result.user)
      } catch (exc) {
        setError(describe(exc))
        throw exc
      }
    },
    [adopt],
  )

  const verifyMfa = useCallback(
    async (code: string) => {
      if (!mfaToken) throw new Error('No MFA challenge in progress')
      setError(null)
      try {
        const result = await authApi.verifyMfa(mfaToken, code)
        tokens.set(result)
        adopt(result.user)
      } catch (exc) {
        setError(describe(exc))
        throw exc
      }
    },
    [adopt, mfaToken],
  )

  const signOut = useCallback(async () => {
    const refresh = tokens.refresh()
    try {
      // Best effort — the server revokes the session family, but a failed
      // logout call must still clear this browser.
      if (refresh) await authApi.logout(refresh)
    } catch {
      /* fall through to the local clear */
    }
    tokens.clear()
    setUser(null)
    setMfaToken(null)
    setStatus('anonymous')
  }, [])

  const value = useMemo<AuthState>(
    () => ({
      status,
      user,
      error,
      forbidden: user !== null && !user.permissions.includes(CONSOLE_PERMISSION),
      signIn,
      devSignIn,
      verifyMfa,
      signOut,
    }),
    [status, user, error, signIn, devSignIn, verifyMfa, signOut],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

function describe(exc: unknown): string {
  if (exc instanceof SessionExpired) return 'Your session ended. Sign in again.'
  if (exc instanceof ApiError) return exc.message
  return 'Could not reach the server.'
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
