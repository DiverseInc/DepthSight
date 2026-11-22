// pwa/lib/useOracleAvailability.ts
//
// THE CLIENT ASKS THE SERVER -- AND IT KEEPS ASKING.
//
// The client asks the SERVER what it can do, and fails closed.
//
// This hook is the only place the PWA fetches the capability, so there is
// one answer for every surface (the launch modal, the strategy editor)
// and they cannot disagree with each other.
//
// WHY IT KEEPS ASKING (the drift that was measured)
// --------------------------------------------------
// It used to ask ONCE per mount -- a plain `useEffect` with an empty
// dependency array -- and never again. The answer could therefore DRIFT
// away from the server for the whole life of the mount: an open screen
// could keep saying "no oracle here" for hours after a real model was
// deployed, and could keep saying "the oracle is available" after the
// model file was taken away. The feature could not light up by itself on
// the day a model was deployed.
//
// The web client already had this right: React Query with a refetch on
// window focus. The PWA has the SAME library wired up -- `pwa/index.tsx`
// mounts a `QueryClientProvider` -- so the PWA now uses it too, with the
// same policy:
//
//   * refetch on mount      a screen that opens asks the server;
//   * refetch every 60s     a model that is deployed (or removed) is
//                            picked up by itself, without the customer
//                            having to reload or pull to refresh;
//   * refetch on foreground `visibilitychange` -- the platform hook a PWA
//                            has -- re-asks when the app comes back.
//
// The answer is never served from cache without the server's word
// (`staleTime: 0`), so a stale "the oracle is available" cannot outlive
// the screen that received it. The interval is paused while the app is in
// the background (React Query's default) and the foreground refetch covers
// that case, so a backgrounded app does not keep calling home.
//
// FAIL CLOSED, ALWAYS
// -------------------
// A probe that has not answered, or that FAILED, is NOT an available
// oracle. `oracleAvailable` stays false until the server says otherwise,
// and `loaded` says whether the probe has answered at all -- so a screen
// can tell "not known yet" from "the server says there is no oracle
// here", and neither of them is ever "yes, the oracle is available".

import { useQuery } from "@tanstack/react-query";
import { api } from "../services/api";
import {
	oracleDefaultConfidencePercent,
	oracleIsUsable,
	type OracleCapability,
} from "./oracleCapability";

/**
 * How often the client re-asks the server what it can do.
 *
 * The capability changes only when a model is deployed or removed, so this
 * is deliberately slow -- but it is NOT "never". A minute is short enough
 * that the feature lights up (or goes dark) by itself, and long enough
 * that it is not a busy poll.
 */
export const ORACLE_AVAILABILITY_REFRESH_MS = 60_000;

export interface OracleAvailability {
	/** The server says a real oracle model is loaded and usable. */
	oracleAvailable: boolean;
	/**
	 * The probe has answered. Until it has, the client shows nothing
	 * about the oracle rather than guessing.
	 */
	loaded: boolean;
	/**
	 * The server's DECLARED confidence threshold, in percent. Null when
	 * the server did not declare one -- and then the client sends no
	 * confidence at all rather than making a number up.
	 */
	defaultConfidence: number | null;
}

export function useOracleAvailability(): OracleAvailability {
	// The answer is the same for every user on a deployment, so the query
	// is not scoped by account: it is the DEPLOYMENT's capability. The
	// shared React Query cache is also what makes "one answer for every
	// surface" literally true -- the launch modal and the strategy editor
	// read the same entry instead of each fetching its own.
	const { data, isFetched } = useQuery<OracleCapability, Error>({
		queryKey: ["capabilities"],
		queryFn: () => api.getCapabilities(),
		// Never serve the answer from cache without the server's word.
		staleTime: 0,
		gcTime: 0,
		// Ask again when the screen mounts...
		refetchOnMount: "always",
		// ...and when the app comes back to the foreground.
		refetchOnWindowFocus: true,
		// ...and at least every minute while the app is open, so the
		// feature lights up by itself the day a model is deployed.
		refetchInterval: ORACLE_AVAILABILITY_REFRESH_MS,
		// A probe that fails IS an answer, and the answer is NO. Retrying
		// it here would only delay the failure; the interval asks again.
		retry: false,
	});

	return {
		// `data` is undefined while the probe is unknown AND after a
		// failed probe, so both fail closed.
		oracleAvailable: oracleIsUsable(data),
		// The probe has answered -- successfully or not.
		loaded: isFetched,
		defaultConfidence: oracleDefaultConfidencePercent(data),
	};
}
