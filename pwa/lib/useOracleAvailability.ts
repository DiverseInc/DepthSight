// pwa/lib/useOracleAvailability.ts
//
// The client asks the SERVER what it can do, once, and fails closed.
//
// This hook is the only place the PWA fetches the capability, so there is
// one answer for every surface (the launch modal, the strategy editor)
// and they cannot disagree with each other.

import { useEffect, useState } from "react";
import { api } from "../services/api";
import {
	oracleDefaultConfidencePercent,
	oracleIsUsable,
	type OracleCapability,
} from "./oracleCapability";

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
	const [capability, setCapability] = useState<OracleCapability | null>(null);

	useEffect(() => {
		let cancelled = false;

		api
			.getCapabilities()
			.then((capabilities) => {
				if (!cancelled) setCapability(capabilities);
			})
			.catch(() => {
				// A probe that could not be completed is NOT an available
				// oracle. Failing closed is what keeps this honest.
				if (!cancelled) setCapability({ oracleAvailable: false });
			});

		return () => {
			cancelled = true;
		};
	}, []);

	return {
		oracleAvailable: oracleIsUsable(capability),
		loaded: capability !== null,
		defaultConfidence: oracleDefaultConfidencePercent(capability),
	};
}
