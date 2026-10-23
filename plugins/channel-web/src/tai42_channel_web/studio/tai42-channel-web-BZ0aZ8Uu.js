import { Button as e, Card as t, Checkbox as n, ConfirmDialog as r, CopyField as i, Dialog as a, EmptyState as o, ErrorState as s, Field as c, ScrollRegion as l, Skeleton as u, Spinner as d, TBody as f, TD as p, TH as m, THead as h, TR as g, Table as _, TextInput as v, coversAnyRoute as y, errorMessage as b, isFullProjection as x, useApi as ee, useCapabilities as te, useOnUnauthorized as ne } from "@tai42/studio-sdk";
import * as S from "react";
import { useRef as re, useState as C } from "react";
import { jsx as w, jsxs as T } from "react/jsx-runtime";
//#region studio-src/mount-base.ts
var ie = null;
function ae(e) {
	ie = e;
}
function oe() {
	return ie;
}
//#endregion
//#region node_modules/.pnpm/@tanstack+react-query@5.104.2_react@19.3.0/node_modules/@tanstack/react-query/build/modern/QueryClientProvider.js
var se = S.createContext(void 0), ce = (e) => {
	let t = S.useContext(se);
	if (e) return e;
	if (!t) throw Error("No QueryClient set, use QueryClientProvider to set one");
	return t;
}, le = ({ client: e, children: t }) => (S.useEffect(() => (e.mount(), () => {
	e.unmount();
}), [e]), /* @__PURE__ */ w(se.Provider, {
	value: e,
	children: t
})), ue = {
	setTimeout: (e, t) => setTimeout(e, t),
	clearTimeout: (e) => clearTimeout(e),
	setInterval: (e, t) => setInterval(e, t),
	clearInterval: (e) => clearInterval(e)
}, E = new class {
	#e = ue;
	setTimeoutProvider(e) {
		this.#e = e;
	}
	setTimeout(e, t) {
		return this.#e.setTimeout(e, t);
	}
	clearTimeout(e) {
		this.#e.clearTimeout(e);
	}
	setInterval(e, t) {
		return this.#e.setInterval(e, t);
	}
	clearInterval(e) {
		this.#e.clearInterval(e);
	}
}();
function de(e) {
	setTimeout(e, 0);
}
//#endregion
//#region node_modules/.pnpm/@tanstack+query-core@5.104.2/node_modules/@tanstack/query-core/build/modern/utils.js
var fe = typeof window > "u" || "Deno" in globalThis;
function D() {}
function pe(e, t) {
	return typeof e == "function" ? e(t) : e;
}
function me(e) {
	return typeof e == "number" && e >= 0 && e !== Infinity;
}
function he(e, t) {
	return Math.max(e + (t || 0) - Date.now(), 0);
}
function O(e, t) {
	return typeof e == "function" ? e(t) : e;
}
function ge(e, t) {
	let { type: n = "all", exact: r, fetchStatus: i, predicate: a, queryKey: o, stale: s } = e;
	if (o) {
		if (r) {
			if (t.queryHash !== ve(o, t.options)) return !1;
		} else if (!ye(t.queryKey, o)) return !1;
	}
	if (n !== "all") {
		let e = t.isActive();
		if (n === "active" && !e || n === "inactive" && e) return !1;
	}
	return !(typeof s == "boolean" && t.isStale() !== s || i && i !== t.state.fetchStatus || a && !a(t));
}
function _e(e, t) {
	let { exact: n, status: r, predicate: i, mutationKey: a } = e;
	if (a) {
		if (!t.options.mutationKey) return !1;
		if (n) {
			if (k(t.options.mutationKey) !== k(a)) return !1;
		} else if (!ye(t.options.mutationKey, a)) return !1;
	}
	return !(r && t.state.status !== r || i && !i(t));
}
function ve(e, t) {
	return (t?.queryKeyHashFn || k)(e);
}
function k(e) {
	return JSON.stringify(e, (e, t) => we(t) ? Object.keys(t).sort().reduce((e, n) => (e[n] = t[n], e), {}) : t);
}
function ye(e, t) {
	if (e === t) return !0;
	if (typeof e != typeof t) return !1;
	if (e && t && typeof e == "object" && typeof t == "object") {
		if (Array.isArray(e) && Array.isArray(t)) {
			if (t.length > e.length) return !1;
			for (let n = 0; n < t.length; n++) if (!ye(e[n], t[n])) return !1;
			return !0;
		}
		let n = Object.keys(t);
		for (let r of n) if (!ye(e[r], t[r])) return !1;
		return !0;
	}
	return !1;
}
var be = Object.prototype.hasOwnProperty;
function xe(e, t, n = 0) {
	if (e === t) return e;
	if (n > 500) return t;
	let r = Ce(e) && Ce(t);
	if (!r && !(we(e) && we(t))) return t;
	let i = (r ? e : Object.keys(e)).length, a = r ? t : Object.keys(t), o = a.length, s = r ? Array(o) : {}, c = 0;
	for (let l = 0; l < o; l++) {
		let o = r ? l : a[l], u = e[o], d = t[o];
		if (u === d) {
			s[o] = u, (r ? l < i : be.call(e, o)) && c++;
			continue;
		}
		if (u === null || d === null || typeof u != "object" || typeof d != "object") {
			s[o] = d;
			continue;
		}
		let f = xe(u, d, n + 1);
		s[o] = f, f === u && c++;
	}
	return i === o && c === i ? e : s;
}
function Se(e, t) {
	if (!t || Object.keys(e).length !== Object.keys(t).length) return !1;
	for (let n in e) if (e[n] !== t[n]) return !1;
	return !0;
}
function Ce(e) {
	return Array.isArray(e) && e.length === Object.keys(e).length;
}
function we(e) {
	if (!Te(e)) return !1;
	let t = Object.getPrototypeOf(e), n = t?.constructor;
	if (n === void 0) return !0;
	if (typeof n != "function") return !1;
	let r = n.prototype;
	return !(!Te(r) || !r.hasOwnProperty("isPrototypeOf") || t !== Object.prototype);
}
function Te(e) {
	return Object.prototype.toString.call(e) === "[object Object]";
}
function Ee(e) {
	return new Promise((t) => {
		E.setTimeout(t, e);
	});
}
function De(e, t, n) {
	return typeof n.structuralSharing == "function" ? n.structuralSharing(e, t) : n.structuralSharing === !1 ? t : xe(e, t);
}
function Oe(e, t, n = 0) {
	let r = [...e, t];
	return n && r.length > n ? r.slice(1) : r;
}
function ke(e, t, n = 0) {
	let r = [t, ...e];
	return n && r.length > n ? r.slice(0, -1) : r;
}
var Ae = Symbol();
function je(e, t) {
	return !e.queryFn && t?.initialPromise ? () => t.initialPromise : !e.queryFn || e.queryFn === Ae ? () => Promise.reject(/* @__PURE__ */ Error(`Missing queryFn: '${e.queryHash}'`)) : e.queryFn;
}
function Me(e, t) {
	return typeof e == "function" ? e(...t) : !!e;
}
function Ne(e, t, n) {
	let r = !1, i;
	return Object.defineProperty(e, "signal", {
		enumerable: !0,
		get: () => (i ??= t(), r || (r = !0, i.aborted ? n() : i.addEventListener("abort", n, { once: !0 })), i)
	}), e;
}
//#endregion
//#region node_modules/.pnpm/@tanstack+query-core@5.104.2/node_modules/@tanstack/query-core/build/modern/environmentManager.js
var Pe = () => fe, Fe = () => Pe(), Ie = class {
	constructor() {
		this.listeners = /* @__PURE__ */ new Set(), this.subscribe = this.subscribe.bind(this);
	}
	subscribe(e) {
		return this.listeners.add(e), this.onSubscribe(), () => {
			this.listeners.delete(e), this.onUnsubscribe();
		};
	}
	hasListeners() {
		return this.listeners.size > 0;
	}
	onSubscribe() {}
	onUnsubscribe() {}
}, Le = new class extends Ie {
	#e;
	#t;
	#n;
	constructor() {
		super(), this.#n = (e) => {
			if (typeof window < "u" && window.addEventListener) {
				let t = () => e();
				return window.addEventListener("visibilitychange", t, !1), () => {
					window.removeEventListener("visibilitychange", t);
				};
			}
		};
	}
	onSubscribe() {
		this.#t || this.setEventListener(this.#n);
	}
	onUnsubscribe() {
		this.hasListeners() || (this.#t?.(), this.#t = void 0);
	}
	setEventListener(e) {
		this.#n = e, this.#t?.(), this.#t = e((e) => {
			typeof e == "boolean" ? this.setFocused(e) : this.onFocus();
		});
	}
	setFocused(e) {
		this.#e !== e && (this.#e = e, this.onFocus());
	}
	onFocus() {
		let e = this.isFocused();
		this.listeners.forEach((t) => {
			t(e);
		});
	}
	isFocused() {
		return typeof this.#e == "boolean" ? this.#e : globalThis.document?.visibilityState !== "hidden";
	}
}(), Re = de;
function ze() {
	let e = [], t = 0, n = (e) => {
		e();
	}, r = (e) => {
		e();
	}, i = Re, a = (r) => {
		t ? e.push(r) : i(() => {
			n(r);
		});
	}, o = () => {
		let t = e;
		e = [], t.length && i(() => {
			r(() => {
				t.forEach((e) => {
					n(e);
				});
			});
		});
	};
	return {
		batch: (e) => {
			let n;
			t++;
			try {
				n = e();
			} finally {
				t--, t || o();
			}
			return n;
		},
		batchCalls: (e) => (...t) => {
			a(() => {
				e(...t);
			});
		},
		schedule: a,
		setNotifyFunction: (e) => {
			n = e;
		},
		setBatchNotifyFunction: (e) => {
			r = e;
		},
		setScheduler: (e) => {
			i = e;
		}
	};
}
var A = ze(), Be = new class extends Ie {
	#e = !0;
	#t;
	#n;
	constructor() {
		super(), this.#n = (e) => {
			if (typeof window < "u" && window.addEventListener) {
				let t = () => e(!0), n = () => e(!1);
				return window.addEventListener("online", t, !1), window.addEventListener("offline", n, !1), () => {
					window.removeEventListener("online", t), window.removeEventListener("offline", n);
				};
			}
		};
	}
	onSubscribe() {
		this.#t || this.setEventListener(this.#n);
	}
	onUnsubscribe() {
		this.hasListeners() || (this.#t?.(), this.#t = void 0);
	}
	setEventListener(e) {
		this.#n = e, this.#t?.(), this.#t = e(this.setOnline.bind(this));
	}
	setOnline(e) {
		this.#e !== e && (this.#e = e, this.listeners.forEach((t) => {
			t(e);
		}));
	}
	isOnline() {
		return this.#e;
	}
}();
//#endregion
//#region node_modules/.pnpm/@tanstack+query-core@5.104.2/node_modules/@tanstack/query-core/build/modern/retryer.js
function Ve(e) {
	return Math.min(1e3 * 2 ** e, 3e4);
}
function He(e) {
	return (e ?? "online") !== "online" || Be.isOnline();
}
var Ue = class extends Error {
	constructor(e) {
		super("CancelledError"), this.revert = e?.revert, this.silent = e?.silent;
	}
};
function We(e) {
	let t = !1, n = 0, r, i = "pending", a, o, s = new Promise((e, t) => {
		a = e, o = t;
	});
	s.catch(D);
	let c = () => i !== "pending", l = (t) => {
		if (!c()) {
			let n = new Ue(t);
			h(n), e.onCancel?.(n);
		}
	}, u = () => {
		t = !0;
	}, d = () => {
		t = !1;
	}, f = () => Le.isFocused() && (e.networkMode === "always" || Be.isOnline()) && e.canRun(), p = () => He(e.networkMode) && e.canRun(), m = (e) => {
		c() || (r?.(), i = "resolved", a(e));
	}, h = (e) => {
		c() || (r?.(), i = "rejected", o(e));
	}, g = (t) => new Promise((n) => {
		r = (e) => {
			(c() || t()) && n(e);
		}, e.onPause?.();
	}).then(() => {
		r = void 0, c() || e.onContinue?.();
	}), _ = () => {
		if (c()) return;
		let r, i = n === 0 ? e.initialPromise : void 0;
		try {
			r = i ?? e.fn();
		} catch (e) {
			r = Promise.reject(e);
		}
		Promise.resolve(r).then(m).catch((r) => {
			if (c()) return;
			let i = e.retry ?? (Fe() ? 0 : 3), a = e.retryDelay ?? Ve, o = typeof a == "function" ? a(n, r) : a, s = i === !0 || typeof i == "number" && n < i || typeof i == "function" && i(n, r);
			t || !s ? h(r) : (n++, e.onFail?.(n, r), Ee(o).then(() => f() ? void 0 : g(f)).then(() => {
				t ? h(r) : _();
			}));
		});
	};
	return {
		promise: s,
		status: () => i,
		cancel: l,
		continue: () => (r?.(), s),
		cancelRetry: u,
		continueRetry: d,
		canStart: p,
		start: () => (p() ? _() : g(p).then(_), s)
	};
}
//#endregion
//#region node_modules/.pnpm/@tanstack+query-core@5.104.2/node_modules/@tanstack/query-core/build/modern/removable.js
var Ge = class {
	#e;
	destroy() {
		this.clearGcTimeout();
	}
	scheduleGc() {
		this.clearGcTimeout(), me(this.gcTime) && (this.#e = E.setTimeout(() => {
			this.optionalRemove();
		}, this.gcTime));
	}
	updateGcTime(e) {
		this.gcTime = Math.max(this.gcTime || 0, e ?? (Fe() ? Infinity : 3e5));
	}
	clearGcTimeout() {
		this.#e !== void 0 && (E.clearTimeout(this.#e), this.#e = void 0);
	}
};
//#endregion
//#region node_modules/.pnpm/@tanstack+query-core@5.104.2/node_modules/@tanstack/query-core/build/modern/infiniteQueryBehavior.js
function Ke(e) {
	return { onFetch: (t, n) => {
		let r = t.options, i = t.fetchOptions?.meta?.fetchMore?.direction, a = t.state.data?.pages || [], o = t.state.data?.pageParams || [], s = {
			pages: [],
			pageParams: []
		}, c = 0, l = async () => {
			let n = !1, l = (e) => {
				Ne(e, () => t.signal, () => n = !0);
			}, u = je(t.options, t.fetchOptions), d = async (e, r, i) => {
				if (n) return Promise.reject(t.signal.reason);
				if (r == null && e.pages.length) return Promise.resolve(e);
				let a = (() => {
					let e = {
						client: t.client,
						queryKey: t.queryKey,
						pageParam: r,
						direction: i ? "backward" : "forward",
						meta: t.options.meta
					};
					return l(e), e;
				})(), o = await u(a), { maxPages: s } = t.options, c = i ? ke : Oe;
				return {
					pages: c(e.pages, o, s),
					pageParams: c(e.pageParams, r, s)
				};
			};
			if (i && a.length) {
				let e = i === "backward", t = e ? Je : qe, n = {
					pages: a,
					pageParams: o
				};
				s = await d(n, t(r, n), e);
			} else {
				let t = e ?? a.length;
				do {
					let e = c === 0 ? o[0] ?? r.initialPageParam : qe(r, s);
					if (c > 0 && e == null) break;
					s = await d(s, e), c++;
				} while (c < t);
			}
			return s;
		};
		t.fetchFn = t.options.persister ? () => t.options.persister?.(l, {
			client: t.client,
			queryKey: t.queryKey,
			meta: t.options.meta,
			signal: t.signal
		}, n) : l;
	} };
}
function qe(e, { pages: t, pageParams: n }) {
	let r = t.length - 1;
	return t.length > 0 ? e.getNextPageParam(t[r], t, n[r], n) : void 0;
}
function Je(e, { pages: t, pageParams: n }) {
	return t.length > 0 ? e.getPreviousPageParam?.(t[0], t, n[0], n) : void 0;
}
//#endregion
//#region node_modules/.pnpm/@tanstack+query-core@5.104.2/node_modules/@tanstack/query-core/build/modern/query.js
var Ye = class extends Ge {
	#e;
	#t;
	#n;
	#r;
	#i;
	#a;
	#o;
	#s;
	constructor(e) {
		super(), this.#s = !1, this.#o = e.defaultOptions, this.setOptions(e.options), this.observers = [], this.#i = e.client, this.#r = this.#i.getQueryCache(), this.queryKey = e.queryKey, this.queryHash = e.queryHash, this.#t = Qe(this.options), this.state = e.state ?? this.#t, this.scheduleGc();
	}
	get meta() {
		return this.options.meta;
	}
	get queryType() {
		return this.#e;
	}
	get promise() {
		return this.#a?.promise;
	}
	setOptions(e) {
		if (this.options = {
			...this.#o,
			...e
		}, e?._type && (this.#e = e._type), this.updateGcTime(this.options.gcTime), this.state && this.state.data === void 0) {
			let e = Qe(this.options);
			e.data !== void 0 && (this.setState(Ze(e.data, e.dataUpdatedAt)), this.#t = e);
		}
	}
	optionalRemove() {
		!this.observers.length && this.state.fetchStatus === "idle" && this.#r.remove(this);
	}
	setData(e, t) {
		let n = De(this.state.data, e, this.options);
		return this.#c({
			data: n,
			type: "success",
			dataUpdatedAt: t?.updatedAt,
			manual: t?.manual
		}), n;
	}
	setState(e) {
		this.#c({
			type: "setState",
			state: e
		});
	}
	cancel(e) {
		let t = this.#a?.promise;
		return this.#a?.cancel(e), t ? t.then(D).catch(D) : Promise.resolve();
	}
	destroy() {
		super.destroy(), this.cancel({ silent: !0 });
	}
	get resetState() {
		return this.#t;
	}
	reset() {
		this.destroy(), this.setState(this.resetState);
	}
	isActive() {
		return this.observers.some((e) => O(e.options.enabled, this) !== !1);
	}
	isDisabled() {
		return this.getObserversCount() > 0 ? !this.isActive() : this.options.queryFn === Ae || !this.isFetched();
	}
	isFetched() {
		return this.state.dataUpdateCount + this.state.errorUpdateCount > 0;
	}
	isStatic() {
		return this.getObserversCount() > 0 && this.observers.some((e) => O(e.options.staleTime, this) === "static");
	}
	isStale() {
		return this.getObserversCount() > 0 ? this.observers.some((e) => e.getCurrentResult().isStale) : this.state.data === void 0 || this.state.isInvalidated;
	}
	isStaleByTime(e = 0) {
		return this.state.data === void 0 ? !0 : e === "static" ? !1 : this.state.isInvalidated ? !0 : !he(this.state.dataUpdatedAt, e);
	}
	onFocus() {
		this.observers.find((e) => e.shouldFetchOnWindowFocus())?.refetch({ cancelRefetch: !1 }), this.#a?.continue();
	}
	onOnline() {
		this.observers.find((e) => e.shouldFetchOnReconnect())?.refetch({ cancelRefetch: !1 }), this.#a?.continue();
	}
	addObserver(e) {
		this.observers.includes(e) || (this.observers.push(e), this.clearGcTimeout(), this.#r.notify({
			type: "observerAdded",
			query: this,
			observer: e
		}));
	}
	removeObserver(e) {
		let t = this.observers.indexOf(e);
		t !== -1 && (this.observers.splice(t, 1), this.observers.length || (this.#a && (this.#s || this.state.fetchStatus === "paused" && this.state.status === "pending" ? this.#a.cancel({ revert: !0 }) : this.#a.cancelRetry()), this.scheduleGc()), this.#r.notify({
			type: "observerRemoved",
			query: this,
			observer: e
		}));
	}
	getObserversCount() {
		return this.observers.length;
	}
	invalidate() {
		this.state.isInvalidated || this.#c({ type: "invalidate" });
	}
	async fetch(e, t) {
		if (this.state.fetchStatus !== "idle" && this.#a?.status() !== "rejected") {
			if (this.state.data !== void 0 && t?.cancelRefetch) this.cancel({ silent: !0 });
			else if (this.#a) return this.#a.continueRetry(), this.#a.promise;
		}
		if (e && this.setOptions(e), !this.options.queryFn) {
			let e = this.observers.find((e) => e.options.queryFn);
			e && this.setOptions(e.options);
		}
		let n = new AbortController(), r = (e) => {
			Object.defineProperty(e, "signal", {
				enumerable: !0,
				get: () => (this.#s = !0, n.signal)
			});
		}, i = () => {
			let e = je(this.options, t), n = (() => {
				let e = {
					client: this.#i,
					queryKey: this.queryKey,
					meta: this.meta
				};
				return r(e), e;
			})();
			return this.#s = !1, this.options.persister ? this.options.persister(e, n, this) : e(n);
		}, a = (() => {
			let e = {
				fetchOptions: t,
				options: this.options,
				queryKey: this.queryKey,
				client: this.#i,
				state: this.state,
				fetchFn: i
			};
			return r(e), e;
		})();
		(this.#e === "infinite" ? Ke(this.options.pages) : this.options.behavior)?.onFetch(a, this), this.#n = this.state, (this.state.fetchStatus === "idle" || this.state.fetchMeta !== a.fetchOptions?.meta) && this.#c({
			type: "fetch",
			meta: a.fetchOptions?.meta
		});
		let o = this.#a = We({
			initialPromise: t?.initialPromise,
			fn: a.fetchFn,
			onCancel: (e) => {
				e instanceof Ue && e.revert && this.setState({
					...this.#n,
					fetchStatus: "idle"
				}), n.abort();
			},
			onFail: (e, t) => {
				this.#c({
					type: "failed",
					failureCount: e,
					error: t
				});
			},
			onPause: () => {
				this.#c({ type: "pause" });
			},
			onContinue: () => {
				this.#c({ type: "continue" });
			},
			retry: a.options.retry,
			retryDelay: a.options.retryDelay,
			networkMode: a.options.networkMode,
			canRun: () => !0
		});
		try {
			let e = await o.start();
			if (e === void 0) throw Error(`${this.queryHash} data is undefined`);
			return this.setData(e), this.#r.config.onSuccess?.(e, this), this.#r.config.onSettled?.(e, this.state.error, this), e;
		} catch (e) {
			if (e instanceof Ue) {
				if (e.silent) return this.#a.promise;
				if (e.revert) {
					if (this.state.data === void 0) throw e;
					return this.state.data;
				}
			}
			throw this.#c({
				type: "error",
				error: e
			}), this.#r.config.onError?.(e, this), this.#r.config.onSettled?.(this.state.data, e, this), e;
		} finally {
			this.#a === o && (this.#a = void 0), this.scheduleGc();
		}
	}
	#c(e) {
		let t = (t) => {
			switch (e.type) {
				case "failed": return {
					...t,
					fetchFailureCount: e.failureCount,
					fetchFailureReason: e.error
				};
				case "pause": return {
					...t,
					fetchStatus: "paused"
				};
				case "continue": return {
					...t,
					fetchStatus: "fetching"
				};
				case "fetch": return {
					...t,
					...Xe(t.data, this.options),
					fetchMeta: e.meta ?? null
				};
				case "success":
					let n = {
						...t,
						...Ze(e.data, e.dataUpdatedAt),
						dataUpdateCount: t.dataUpdateCount + 1,
						...!e.manual && {
							fetchStatus: "idle",
							fetchFailureCount: 0,
							fetchFailureReason: null
						}
					};
					return this.#n = e.manual ? n : void 0, n;
				case "error":
					let r = e.error;
					return {
						...t,
						error: r,
						errorUpdateCount: t.errorUpdateCount + 1,
						errorUpdatedAt: Date.now(),
						fetchFailureCount: t.fetchFailureCount + 1,
						fetchFailureReason: r,
						fetchStatus: "idle",
						status: "error",
						isInvalidated: !0
					};
				case "invalidate": return {
					...t,
					isInvalidated: !0
				};
				case "setState": return {
					...t,
					...e.state
				};
			}
		};
		this.state = t(this.state), A.batch(() => {
			this.observers.slice().forEach((e) => {
				e.onQueryUpdate();
			}), this.#r.notify({
				query: this,
				type: "updated",
				action: e
			});
		});
	}
};
function Xe(e, t) {
	return {
		fetchFailureCount: 0,
		fetchFailureReason: null,
		fetchStatus: He(t.networkMode) ? "fetching" : "paused",
		...e === void 0 && {
			error: null,
			status: "pending"
		}
	};
}
function Ze(e, t) {
	return {
		data: e,
		dataUpdatedAt: t ?? Date.now(),
		error: null,
		isInvalidated: !1,
		status: "success"
	};
}
function Qe(e) {
	let t = typeof e.initialData == "function" ? e.initialData() : e.initialData, n = t !== void 0, r = n ? typeof e.initialDataUpdatedAt == "function" ? e.initialDataUpdatedAt() : e.initialDataUpdatedAt : 0;
	return {
		data: t,
		dataUpdateCount: 0,
		dataUpdatedAt: n ? r ?? Date.now() : 0,
		error: null,
		errorUpdateCount: 0,
		errorUpdatedAt: 0,
		fetchFailureCount: 0,
		fetchFailureReason: null,
		fetchMeta: null,
		isInvalidated: !1,
		status: n ? "success" : "pending",
		fetchStatus: "idle"
	};
}
//#endregion
//#region node_modules/.pnpm/@tanstack+query-core@5.104.2/node_modules/@tanstack/query-core/build/modern/queryObserver.js
var $e = class extends Ie {
	#e;
	#t = void 0;
	#n = void 0;
	#r = void 0;
	#i;
	#a;
	#o;
	#s;
	#c;
	#l;
	#u;
	#d;
	#f;
	#p = /* @__PURE__ */ new Set();
	constructor(e, t) {
		super(), this.options = t, this.#e = e, this.#o = null, this.bindMethods(), this.setOptions(t);
	}
	bindMethods() {
		this.refetch = this.refetch.bind(this);
	}
	onSubscribe() {
		this.listeners.size === 1 && (this.#t.addObserver(this), tt(this.#t, this.options) ? this.#m() : this.updateResult(), this.#y());
	}
	onUnsubscribe() {
		this.hasListeners() || this.destroy();
	}
	shouldFetchOnReconnect() {
		return nt(this.#t, this.options, this.options.refetchOnReconnect);
	}
	shouldFetchOnWindowFocus() {
		return nt(this.#t, this.options, this.options.refetchOnWindowFocus);
	}
	destroy() {
		this.listeners = /* @__PURE__ */ new Set(), this.#b(), this.#x(), this.#t.removeObserver(this);
	}
	setOptions(e) {
		let t = this.options, n = this.#t;
		if (this.options = this.#e.defaultQueryOptions(e), this.options.enabled !== void 0 && typeof this.options.enabled != "boolean" && typeof this.options.enabled != "function" && typeof O(this.options.enabled, this.#t) != "boolean") throw Error("Expected enabled to be a boolean or a callback that returns a boolean");
		this.#S(), this.#t.setOptions(this.options), t._defaulted && !Se(this.options, t) && this.#e.getQueryCache().notify({
			type: "observerOptionsUpdated",
			query: this.#t,
			observer: this
		});
		let r = this.hasListeners();
		r && rt(this.#t, n, this.options, t) && this.#m(), this.updateResult(), r && (this.#t !== n || O(this.options.enabled, this.#t) !== O(t.enabled, this.#t) || O(this.options.staleTime, this.#t) !== O(t.staleTime, this.#t)) && this.#g();
		let i = this.#_();
		r && (this.#t !== n || O(this.options.enabled, this.#t) !== O(t.enabled, this.#t) || i !== this.#f) && this.#v(i);
	}
	getOptimisticResult(e) {
		let t = this.#e.getQueryCache().build(this.#e, e), n = this.createResult(t, e);
		return Se(this.getCurrentResult(), n) || (this.#r = n, this.#a = this.options, this.#i = this.#t.state), n;
	}
	getCurrentResult() {
		return this.#r;
	}
	trackResult(e, t) {
		return new Proxy(e, { get: (e, n) => (this.trackProp(n), t?.(n), Reflect.get(e, n)) });
	}
	trackProp(e) {
		this.#p.add(e);
	}
	getCurrentQuery() {
		return this.#t;
	}
	refetch({ ...e } = {}) {
		return this.fetch({ ...e });
	}
	fetchOptimistic(e) {
		let t = this.#e.defaultQueryOptions(e), n = this.#e.getQueryCache().build(this.#e, t), r = () => {}, i, a = new Promise((e) => {
			i = e, r = this.#e.getQueryCache().subscribe((i) => {
				i.type === "updated" && i.query.queryHash === n.queryHash && n.state.data !== void 0 && (r(), e(this.createResult(n, t)));
			});
		});
		return Promise.race([n.fetch().then(() => {
			let e = this.createResult(n, t);
			return i?.(e), e;
		}).finally(() => {
			r();
		}), a]);
	}
	fetch(e) {
		return this.#m({
			...e,
			cancelRefetch: e.cancelRefetch ?? !0
		}).then(() => (this.updateResult(), this.#r));
	}
	#m(e) {
		this.#S();
		let t = this.#t.fetch(this.options, e);
		return e?.throwOnError || (t = t.catch(D)), t;
	}
	#h(e) {
		return !Fe() && O(this.options.enabled, this.#t) !== !1 && me(e);
	}
	#g() {
		this.#b();
		let e = O(this.options.staleTime, this.#t);
		if (this.#r.isStale || !this.#h(e)) return;
		let t = he(this.#r.dataUpdatedAt, e) + 1;
		this.#u = E.setTimeout(() => {
			this.#r.isStale || this.updateResult();
		}, t);
	}
	#_() {
		return O(this.options.refetchInterval, this.#t) ?? !1;
	}
	#v(e) {
		this.#x(), this.#f = e, this.#f !== 0 && this.#h(this.#f) && (this.#d = E.setInterval(() => {
			(this.options.refetchIntervalInBackground || Le.isFocused()) && this.#m();
		}, this.#f));
	}
	#y() {
		this.#g(), this.#v(this.#_());
	}
	#b() {
		this.#u !== void 0 && (E.clearTimeout(this.#u), this.#u = void 0);
	}
	#x() {
		this.#d !== void 0 && (E.clearInterval(this.#d), this.#d = void 0);
	}
	createResult(e, t) {
		let n = this.#t, r = this.options, i = this.#r, a = this.#i, o = this.#a, s = e === n ? this.#n : e.state, { state: c } = e, l = { ...c }, u = !1, d;
		if (t._optimisticResults) {
			let i = this.hasListeners(), a = !i && tt(e, t), o = i && rt(e, n, t, r);
			(a || o) && (l = {
				...l,
				...Xe(c.data, e.options)
			}), t._optimisticResults === "isRestoring" && (l.fetchStatus = "idle");
		}
		let { error: f, errorUpdatedAt: p, status: m } = l;
		d = l.data;
		let h = !1;
		if (t.placeholderData !== void 0 && d === void 0 && m === "pending") {
			let e;
			i?.isPlaceholderData && t.placeholderData === o?.placeholderData ? (e = i.data, h = !0) : e = typeof t.placeholderData == "function" ? t.placeholderData(this.#l?.state.data, this.#l) : t.placeholderData, e !== void 0 && (m = "success", d = De(i?.data, e, t), u = !0);
		}
		if (t.select && d !== void 0 && !h) {
			if (i && d === a?.data && t.select === this.#s) d = this.#c;
			else try {
				this.#s = t.select, d = t.select(d), d = De(i?.data, d, t), this.#c = d, this.#o = null;
			} catch (e) {
				this.#o = e;
			}
		} else d === void 0 && (this.#o = null);
		this.#o && (f = this.#o, d = this.#c, p = Date.now(), m = "error", u = !1);
		let g = l.fetchStatus === "fetching", _ = m === "pending", v = m === "error", y = _ && g, b = d !== void 0;
		return {
			status: m,
			fetchStatus: l.fetchStatus,
			isPending: _,
			isSuccess: m === "success",
			isError: v,
			isInitialLoading: y,
			isLoading: y,
			data: d,
			dataUpdatedAt: l.dataUpdatedAt,
			error: f,
			errorUpdatedAt: p,
			failureCount: l.fetchFailureCount,
			failureReason: l.fetchFailureReason,
			errorUpdateCount: l.errorUpdateCount,
			isFetched: e.isFetched(),
			isFetchedAfterMount: l.dataUpdateCount > s.dataUpdateCount || l.errorUpdateCount > s.errorUpdateCount,
			isFetching: g,
			isRefetching: g && !_,
			isLoadingError: v && !b,
			isPaused: l.fetchStatus === "paused",
			isPlaceholderData: u,
			isRefetchError: v && b,
			isStale: it(e, t),
			refetch: this.refetch,
			isEnabled: O(t.enabled, e) !== !1
		};
	}
	updateResult() {
		let e = this.#r, t = this.createResult(this.#t, this.options);
		if (this.#i = this.#t.state, this.#a = this.options, this.#i.data !== void 0 && (this.#l = this.#t), Se(t, e)) return;
		this.#r = t;
		let n = (() => {
			if (!e) return !0;
			let { notifyOnChangeProps: t } = this.options, n = typeof t == "function" ? t() : t;
			if (n === "all" || !n && !this.#p.size) return !0;
			let r = new Set(n ?? this.#p);
			return this.options.throwOnError && r.add("error"), Object.keys(this.#r).some((t) => {
				let n = t;
				return this.#r[n] !== e[n] && r.has(n);
			});
		})();
		A.batch(() => {
			n && this.listeners.forEach((e) => {
				e(this.#r);
			}), this.#e.getQueryCache().notify({
				query: this.#t,
				type: "observerResultsUpdated"
			});
		});
	}
	#S() {
		let e = this.#e.getQueryCache().build(this.#e, this.options);
		if (e === this.#t) return;
		let t = this.#t;
		this.#t = e, this.#n = e.state, this.hasListeners() && (t?.removeObserver(this), e.addObserver(this));
	}
	onQueryUpdate() {
		this.updateResult(), this.hasListeners() && this.#y();
	}
};
function et(e, t) {
	return O(t.enabled, e) !== !1 && e.state.data === void 0 && (e.state.status !== "error" || O(t.retryOnMount, e) !== !1);
}
function tt(e, t) {
	return et(e, t) || e.state.data !== void 0 && nt(e, t, t.refetchOnMount);
}
function nt(e, t, n) {
	if (O(t.enabled, e) !== !1 && O(t.staleTime, e) !== "static") {
		let r = O(n, e);
		return r === "always" || r !== !1 && it(e, t);
	}
	return !1;
}
function rt(e, t, n, r) {
	return (e !== t || O(r.enabled, e) === !1) && (!n.suspense || e.state.status !== "error") && it(e, n);
}
function it(e, t) {
	return O(t.enabled, e) !== !1 && e.isStaleByTime(O(t.staleTime, e));
}
//#endregion
//#region node_modules/.pnpm/@tanstack+query-core@5.104.2/node_modules/@tanstack/query-core/build/modern/mutation.js
var at = class extends Ge {
	#e;
	#t;
	#n;
	#r;
	constructor(e) {
		super(), this.#e = e.client, this.mutationId = e.mutationId, this.#n = e.mutationCache, this.#t = [], this.state = e.state || ot(), this.setOptions(e.options), this.scheduleGc();
	}
	setOptions(e) {
		this.options = e, this.updateGcTime(this.options.gcTime);
	}
	get meta() {
		return this.options.meta;
	}
	addObserver(e) {
		this.#t.includes(e) || (this.#t.push(e), this.clearGcTimeout(), this.#n.notify({
			type: "observerAdded",
			mutation: this,
			observer: e
		}));
	}
	removeObserver(e) {
		this.#t = this.#t.filter((t) => t !== e), this.scheduleGc(), this.#n.notify({
			type: "observerRemoved",
			mutation: this,
			observer: e
		});
	}
	optionalRemove() {
		this.#t.length || (this.state.status === "pending" ? this.scheduleGc() : this.#n.remove(this));
	}
	continue() {
		return this.#r?.continue() ?? (this.state.status === "pending" ? this.execute(this.state.variables) : Promise.resolve());
	}
	async execute(e) {
		let t = () => {
			this.#i({ type: "continue" });
		}, n = {
			client: this.#e,
			meta: this.options.meta,
			mutationKey: this.options.mutationKey
		}, r = this.#r = We({
			fn: () => this.options.mutationFn ? this.options.mutationFn(e, n) : Promise.reject(/* @__PURE__ */ Error("No mutationFn found")),
			onFail: (e, t) => {
				this.#i({
					type: "failed",
					failureCount: e,
					error: t
				});
			},
			onPause: () => {
				this.#i({ type: "pause" });
			},
			onContinue: t,
			retry: this.options.retry ?? 0,
			retryDelay: this.options.retryDelay,
			networkMode: this.options.networkMode,
			canRun: () => this.#n.canRun(this)
		}), i = this.state.status === "pending", a = !r.canStart();
		try {
			if (i) t();
			else {
				this.#i({
					type: "pending",
					variables: e,
					isPaused: a
				}), this.#n.config.onMutate && await this.#n.config.onMutate(e, this, n);
				let t = await this.options.onMutate?.(e, n);
				t !== this.state.context && this.#i({
					type: "pending",
					context: t,
					variables: e,
					isPaused: a
				});
			}
			let o = await r.start();
			return await this.#n.config.onSuccess?.(o, e, this.state.context, this, n), await this.options.onSuccess?.(o, e, this.state.context, n), await this.#n.config.onSettled?.(o, null, this.state.variables, this.state.context, this, n), await this.options.onSettled?.(o, null, e, this.state.context, n), this.#i({
				type: "success",
				data: o
			}), o;
		} catch (t) {
			try {
				await this.#n.config.onError?.(t, e, this.state.context, this, n);
			} catch (e) {
				Promise.reject(e);
			}
			try {
				await this.options.onError?.(t, e, this.state.context, n);
			} catch (e) {
				Promise.reject(e);
			}
			try {
				await this.#n.config.onSettled?.(void 0, t, this.state.variables, this.state.context, this, n);
			} catch (e) {
				Promise.reject(e);
			}
			try {
				await this.options.onSettled?.(void 0, t, e, this.state.context, n);
			} catch (e) {
				Promise.reject(e);
			}
			throw this.#i({
				type: "error",
				error: t
			}), t;
		} finally {
			this.#r === r && (this.#r = void 0), this.#n.runNext(this);
		}
	}
	#i(e) {
		let t = (t) => {
			switch (e.type) {
				case "failed": return {
					...t,
					failureCount: e.failureCount,
					failureReason: e.error
				};
				case "pause": return {
					...t,
					isPaused: !0
				};
				case "continue": return {
					...t,
					isPaused: !1
				};
				case "pending": return {
					...t,
					context: e.context,
					data: void 0,
					failureCount: 0,
					failureReason: null,
					error: null,
					isPaused: e.isPaused,
					status: "pending",
					variables: e.variables,
					submittedAt: Date.now()
				};
				case "success": return {
					...t,
					data: e.data,
					failureCount: 0,
					failureReason: null,
					error: null,
					status: "success",
					isPaused: !1
				};
				case "error": return {
					...t,
					data: void 0,
					error: e.error,
					failureCount: t.failureCount + 1,
					failureReason: e.error,
					isPaused: !1,
					status: "error"
				};
			}
		};
		this.state = t(this.state), A.batch(() => {
			this.#t.forEach((t) => {
				t.onMutationUpdate(e);
			}), this.#n.notify({
				mutation: this,
				type: "updated",
				action: e
			});
		});
	}
};
function ot() {
	return {
		context: void 0,
		data: void 0,
		error: null,
		failureCount: 0,
		failureReason: null,
		isPaused: !1,
		status: "idle",
		variables: void 0,
		submittedAt: 0
	};
}
//#endregion
//#region node_modules/.pnpm/@tanstack+query-core@5.104.2/node_modules/@tanstack/query-core/build/modern/mutationCache.js
var st = class extends Ie {
	#e;
	#t;
	#n;
	constructor(e = {}) {
		super(), this.config = e, this.#e = /* @__PURE__ */ new Set(), this.#t = /* @__PURE__ */ new Map(), this.#n = 0;
	}
	build(e, t, n) {
		let r = new at({
			client: e,
			mutationCache: this,
			mutationId: ++this.#n,
			options: e.defaultMutationOptions(t),
			state: n
		});
		return this.add(r), r;
	}
	add(e) {
		this.#e.add(e);
		let t = ct(e);
		if (typeof t == "string") {
			let n = this.#t.get(t);
			n ? n.push(e) : this.#t.set(t, [e]);
		}
		this.notify({
			type: "added",
			mutation: e
		});
	}
	remove(e) {
		if (this.#e.delete(e)) {
			let t = ct(e);
			if (typeof t == "string") {
				let n = this.#t.get(t);
				if (n) {
					if (n.length > 1) {
						let t = n.indexOf(e);
						t !== -1 && n.splice(t, 1);
					} else n[0] === e && this.#t.delete(t);
				}
			}
		}
		this.notify({
			type: "removed",
			mutation: e
		});
	}
	canRun(e) {
		let t = ct(e);
		if (typeof t == "string") {
			let n = this.#t.get(t)?.find((e) => e.state.status === "pending");
			return !n || n === e;
		}
		return !0;
	}
	runNext(e) {
		let t = ct(e);
		return typeof t == "string" ? (this.#t.get(t)?.find((t) => t !== e && t.state.isPaused))?.continue() ?? Promise.resolve() : Promise.resolve();
	}
	clear() {
		A.batch(() => {
			this.#e.forEach((e) => {
				this.notify({
					type: "removed",
					mutation: e
				});
			}), this.#e.clear(), this.#t.clear();
		});
	}
	getAll() {
		return Array.from(this.#e);
	}
	find(e) {
		let t = {
			exact: !0,
			...e
		};
		return this.getAll().find((e) => _e(t, e));
	}
	findAll(e = {}) {
		return this.getAll().filter((t) => _e(e, t));
	}
	notify(e) {
		A.batch(() => {
			this.listeners.forEach((t) => {
				t(e);
			});
		});
	}
	resumePausedMutations() {
		let e = this.getAll().filter((e) => e.state.isPaused);
		return A.batch(() => Promise.all(e.map((e) => e.continue().catch(D))));
	}
};
function ct(e) {
	return e.options.scope?.id;
}
//#endregion
//#region node_modules/.pnpm/@tanstack+query-core@5.104.2/node_modules/@tanstack/query-core/build/modern/mutationObserver.js
var lt = class extends Ie {
	#e;
	#t = void 0;
	#n;
	#r;
	constructor(e, t) {
		super(), this.#e = e, this.setOptions(t), this.bindMethods(), this.#i();
	}
	bindMethods() {
		this.mutate = this.mutate.bind(this), this.reset = this.reset.bind(this);
	}
	setOptions(e) {
		let t = this.options;
		this.options = this.#e.defaultMutationOptions(e), Se(this.options, t) || this.#e.getMutationCache().notify({
			type: "observerOptionsUpdated",
			mutation: this.#n,
			observer: this
		}), t?.mutationKey && this.options.mutationKey && k(t.mutationKey) !== k(this.options.mutationKey) ? this.reset() : this.#n?.state.status === "pending" && this.#n.setOptions(this.options);
	}
	onSubscribe() {
		this.listeners.size === 1 && this.#n && (this.#n.addObserver(this), this.#i());
	}
	onUnsubscribe() {
		this.hasListeners() || this.#n?.removeObserver(this);
	}
	onMutationUpdate(e) {
		this.#i(), this.#a(e);
	}
	getCurrentResult() {
		return this.#t;
	}
	reset() {
		this.#n?.removeObserver(this), this.#n = void 0, this.#i(), this.#a();
	}
	mutate(e, t) {
		return this.#r = t, this.#n?.removeObserver(this), this.#n = this.#e.getMutationCache().build(this.#e, this.options), this.#n.addObserver(this), this.#n.execute(e);
	}
	#i() {
		let e = this.#n?.state ?? ot();
		this.#t = {
			...e,
			isPending: e.status === "pending",
			isSuccess: e.status === "success",
			isError: e.status === "error",
			isIdle: e.status === "idle",
			mutate: this.mutate,
			reset: this.reset
		};
	}
	#a(e) {
		A.batch(() => {
			let t = this.#r;
			if (t && this.hasListeners()) {
				let n = this.#t.variables, r = this.#t.context, i = {
					client: this.#e,
					meta: this.options.meta,
					mutationKey: this.options.mutationKey
				};
				if (e?.type === "success") {
					try {
						t.onSuccess?.(e.data, n, r, i);
					} catch (e) {
						Promise.reject(e);
					}
					try {
						t.onSettled?.(e.data, null, n, r, i);
					} catch (e) {
						Promise.reject(e);
					}
				} else if (e?.type === "error") {
					try {
						t.onError?.(e.error, n, r, i);
					} catch (e) {
						Promise.reject(e);
					}
					try {
						t.onSettled?.(void 0, e.error, n, r, i);
					} catch (e) {
						Promise.reject(e);
					}
				}
			}
			this.listeners.forEach((e) => {
				e(this.#t);
			});
		});
	}
}, ut = class extends Ie {
	#e;
	constructor(e = {}) {
		super(), this.config = e, this.#e = /* @__PURE__ */ new Map();
	}
	build(e, t, n) {
		let r = t.queryKey, i = t.queryHash ?? ve(r, t), a = this.get(i);
		return a || (a = new Ye({
			client: e,
			queryKey: r,
			queryHash: i,
			options: e.defaultQueryOptions(t),
			state: n,
			defaultOptions: e.getQueryDefaults(r)
		}), this.add(a)), a;
	}
	add(e) {
		this.#e.has(e.queryHash) || (this.#e.set(e.queryHash, e), this.notify({
			type: "added",
			query: e
		}));
	}
	remove(e) {
		this.#e.get(e.queryHash) === e && (e.destroy(), this.#e.delete(e.queryHash), this.notify({
			type: "removed",
			query: e
		}));
	}
	clear() {
		A.batch(() => {
			this.getAll().forEach((e) => {
				this.remove(e);
			});
		});
	}
	get(e) {
		return this.#e.get(e);
	}
	getAll() {
		return [...this.#e.values()];
	}
	find(e) {
		let t = {
			exact: !0,
			...e
		};
		return this.getAll().find((e) => ge(t, e));
	}
	findAll(e = {}) {
		let t = this.getAll();
		return Object.keys(e).length > 0 ? t.filter((t) => ge(e, t)) : t;
	}
	notify(e) {
		A.batch(() => {
			this.listeners.forEach((t) => {
				t(e);
			});
		});
	}
	onFocus() {
		A.batch(() => {
			this.getAll().forEach((e) => {
				e.onFocus();
			});
		});
	}
	onOnline() {
		A.batch(() => {
			this.getAll().forEach((e) => {
				e.onOnline();
			});
		});
	}
}, dt = class {
	#e;
	#t;
	#n;
	#r;
	#i;
	#a;
	#o;
	#s;
	constructor(e = {}) {
		this.#e = e.queryCache || new ut(), this.#t = e.mutationCache || new st(), this.#n = e.defaultOptions || {}, this.#r = /* @__PURE__ */ new Map(), this.#i = /* @__PURE__ */ new Map(), this.#a = 0;
	}
	mount() {
		this.#a++, this.#a === 1 && (this.#o = Le.subscribe(async (e) => {
			e && (await this.resumePausedMutations(), this.#e.onFocus());
		}), this.#s = Be.subscribe(async (e) => {
			e && (await this.resumePausedMutations(), this.#e.onOnline());
		}));
	}
	unmount() {
		this.#a--, this.#a === 0 && (this.#o?.(), this.#o = void 0, this.#s?.(), this.#s = void 0);
	}
	isFetching(e) {
		return this.#e.findAll({
			...e,
			fetchStatus: "fetching"
		}).length;
	}
	isMutating(e) {
		return this.#t.findAll({
			...e,
			status: "pending"
		}).length;
	}
	getQueryData(e) {
		let t = this.defaultQueryOptions({ queryKey: e });
		return this.#e.get(t.queryHash)?.state.data;
	}
	ensureQueryData(e) {
		let t = this.defaultQueryOptions(e), n = this.#e.build(this, t), r = n.state.data;
		return r === void 0 ? this.fetchQuery(e) : (e.revalidateIfStale && n.isStaleByTime(O(t.staleTime, n)) && this.prefetchQuery(t), Promise.resolve(r));
	}
	getQueriesData(e) {
		return this.#e.findAll(e).map(({ queryKey: e, state: t }) => [e, t.data]);
	}
	setQueryData(e, t, n) {
		let r = this.defaultQueryOptions({ queryKey: e }), i = this.#e.get(r.queryHash)?.state.data, a = pe(t, i);
		if (a !== void 0) return this.#e.build(this, r).setData(a, {
			...n,
			manual: !0
		});
	}
	setQueriesData(e, t, n) {
		return A.batch(() => this.#e.findAll(e).map(({ queryKey: e }) => [e, this.setQueryData(e, t, n)]));
	}
	getQueryState(e) {
		let t = this.defaultQueryOptions({ queryKey: e });
		return this.#e.get(t.queryHash)?.state;
	}
	removeQueries(e) {
		let t = this.#e;
		A.batch(() => {
			t.findAll(e).forEach((e) => {
				t.remove(e);
			});
		});
	}
	resetQueries(e, t) {
		let n = this.#e;
		return A.batch(() => {
			let r = n.findAll(e), i = new Set(r);
			return r.forEach((e) => {
				e.reset();
			}), this.refetchQueries({
				type: "active",
				predicate: (e) => i.has(e)
			}, t);
		});
	}
	cancelQueries(e, t = {}) {
		let n = {
			revert: !0,
			...t
		}, r = A.batch(() => this.#e.findAll(e).map((e) => e.cancel(n)));
		return Promise.all(r).then(D).catch(D);
	}
	invalidateQueries(e, t = {}) {
		return A.batch(() => (this.#e.findAll(e).forEach((e) => {
			e.invalidate();
		}), e?.refetchType === "none" ? Promise.resolve() : this.refetchQueries({
			...e,
			type: e?.refetchType ?? e?.type ?? "active"
		}, t)));
	}
	refetchQueries(e, t = {}) {
		let n = {
			...t,
			cancelRefetch: t.cancelRefetch ?? !0
		}, r = A.batch(() => this.#e.findAll(e).filter((e) => !e.isDisabled() && !e.isStatic()).map((e) => {
			let t = e.fetch(void 0, n);
			return n.throwOnError || (t = t.catch(D)), e.state.fetchStatus === "paused" ? Promise.resolve() : t;
		}));
		return Promise.all(r).then(D);
	}
	async query(e) {
		let t = this.defaultQueryOptions(e);
		t.retry === void 0 && (t.retry = !1);
		let n = this.#e.build(this, t), r = n.isStaleByTime(O(t.staleTime, n)) ? await n.fetch(t) : n.state.data, i = t.select;
		return i ? i(r) : r;
	}
	fetchQuery(e) {
		let t = this.defaultQueryOptions(e);
		t.retry === void 0 && (t.retry = !1);
		let n = this.#e.build(this, t);
		return n.isStaleByTime(O(t.staleTime, n)) ? n.fetch(t) : Promise.resolve(n.state.data);
	}
	prefetchQuery(e) {
		return this.fetchQuery(e).then(D).catch(D);
	}
	infiniteQuery(e) {
		return e._type = "infinite", this.query(e);
	}
	fetchInfiniteQuery(e) {
		return e._type = "infinite", this.fetchQuery(e);
	}
	prefetchInfiniteQuery(e) {
		return this.fetchInfiniteQuery(e).then(D).catch(D);
	}
	ensureInfiniteQueryData(e) {
		return e._type = "infinite", this.ensureQueryData(e);
	}
	resumePausedMutations() {
		return Be.isOnline() ? this.#t.resumePausedMutations() : Promise.resolve();
	}
	getQueryCache() {
		return this.#e;
	}
	getMutationCache() {
		return this.#t;
	}
	getDefaultOptions() {
		return this.#n;
	}
	setDefaultOptions(e) {
		this.#n = e;
	}
	setQueryDefaults(e, t) {
		this.#r.set(k(e), {
			queryKey: e,
			defaultOptions: t
		});
	}
	getQueryDefaults(e) {
		let t = [...this.#r.values()], n = {};
		return t.forEach((t) => {
			ye(e, t.queryKey) && Object.assign(n, t.defaultOptions);
		}), n;
	}
	setMutationDefaults(e, t) {
		this.#i.set(k(e), {
			mutationKey: e,
			defaultOptions: t
		});
	}
	getMutationDefaults(e) {
		let t = [...this.#i.values()], n = {};
		return t.forEach((t) => {
			ye(e, t.mutationKey) && Object.assign(n, t.defaultOptions);
		}), n;
	}
	defaultQueryOptions(e) {
		if (e._defaulted) return e;
		let t = {
			...this.#n.queries,
			...this.getQueryDefaults(e.queryKey),
			...e,
			_defaulted: !0
		};
		return t.queryHash ||= ve(t.queryKey, t), t.refetchOnReconnect === void 0 && (t.refetchOnReconnect = t.networkMode !== "always"), t.throwOnError === void 0 && (t.throwOnError = !!t.suspense), !t.networkMode && t.persister && (t.networkMode = "offlineFirst"), t.queryFn === Ae && (t.enabled = !1), t;
	}
	defaultMutationOptions(e) {
		return e?._defaulted ? e : {
			...this.#n.mutations,
			...e?.mutationKey && this.getMutationDefaults(e.mutationKey),
			...e,
			_defaulted: !0
		};
	}
	clear() {
		this.#e.clear(), this.#t.clear();
	}
}, ft = S.createContext(!1), pt = () => S.useContext(ft);
ft.Provider;
//#endregion
//#region node_modules/.pnpm/@tanstack+react-query@5.104.2_react@19.3.0/node_modules/@tanstack/react-query/build/modern/QueryErrorResetBoundary.js
function mt() {
	let e = !1;
	return {
		clearReset: () => {
			e = !1;
		},
		reset: () => {
			e = !0;
		},
		isReset: () => e
	};
}
var ht = S.createContext(mt()), gt = () => S.useContext(ht), _t = (e, t, n) => {
	let r = n?.state.error && typeof e.throwOnError == "function" ? Me(e.throwOnError, [n.state.error, n]) : e.throwOnError;
	(e.suspense || r) && (t.isReset() || (e.retryOnMount = !1));
}, vt = (e) => {
	S.useEffect(() => {
		e.clearReset();
	}, [e]);
}, yt = ({ result: e, errorResetBoundary: t, throwOnError: n, query: r, suspense: i }) => e.isError && !t.isReset() && !e.isFetching && r && (i && e.data === void 0 || Me(n, [e.error, r])), bt = (e) => {
	if (e.suspense) {
		let t = 1e3, n = (e) => e === "static" ? e : Math.max(e ?? t, t), r = e.staleTime;
		e.staleTime = typeof r == "function" ? (...e) => n(r(...e)) : n(r), typeof e.gcTime == "number" && (e.gcTime = Math.max(e.gcTime, t));
	}
}, xt = (e, t) => e?.suspense && t.isPending, St = (e, t, n) => t.fetchOptimistic(e).catch(() => {
	n.clearReset();
});
//#endregion
//#region node_modules/.pnpm/@tanstack+react-query@5.104.2_react@19.3.0/node_modules/@tanstack/react-query/build/modern/useBaseQuery.js
function Ct(e, t, n) {
	let r = pt(), i = gt(), a = ce(n), o = a.defaultQueryOptions(e), s = a.getQueryCache().get(o.queryHash), c = e.subscribed !== !1;
	o._optimisticResults = r ? "isRestoring" : c ? "optimistic" : void 0, bt(o), _t(o, i, s), vt(i);
	let [l] = S.useState(() => new t(a, o)), u = l.getOptimisticResult(o), d = !r && c;
	if (S.useSyncExternalStore(S.useCallback((e) => {
		let t = d ? l.subscribe(A.batchCalls(e)) : D;
		return l.updateResult(), t;
	}, [l, d]), () => l.getCurrentResult(), () => l.getCurrentResult()), S.useEffect(() => {
		l.setOptions(o);
	}, [o, l]), xt(o, u)) throw St(o, l, i);
	if (yt({
		result: u,
		errorResetBoundary: i,
		throwOnError: o.throwOnError,
		query: s,
		suspense: o.suspense
	})) throw u.error;
	return o.notifyOnChangeProps ? u : l.trackResult(u);
}
//#endregion
//#region node_modules/.pnpm/@tanstack+react-query@5.104.2_react@19.3.0/node_modules/@tanstack/react-query/build/modern/useQuery.js
function wt(e, t) {
	return Ct(e, $e, t);
}
//#endregion
//#region node_modules/.pnpm/@tanstack+react-query@5.104.2_react@19.3.0/node_modules/@tanstack/react-query/build/modern/useMutation.js
function Tt(e, t) {
	let n = ce(t), [r] = S.useState(() => new lt(n, e));
	S.useEffect(() => {
		r.setOptions(e);
	}, [r, e]);
	let i = S.useSyncExternalStore(S.useCallback((e) => r.subscribe(A.batchCalls(e)), [r]), () => r.getCurrentResult(), () => r.getCurrentResult()), a = S.useCallback((...e) => {
		r.mutate(e[0], e[1]).catch(D);
	}, [r]);
	if (i.error && Me(r.options.throwOnError, [i.error])) throw i.error;
	return {
		...i,
		mutate: a,
		mutateAsync: i.mutate
	};
}
//#endregion
//#region studio-src/format.ts
function Et(e) {
	let t = new Date(e);
	return Number.isNaN(t.getTime()) ? e : t.toLocaleString();
}
//#endregion
//#region studio-src/EntryCodesTable.tsx
function Dt(e) {
	return e === null ? "Never" : Et(e);
}
function Ot({ codes: t, onRevoke: n }) {
	return t.length === 0 ? /* @__PURE__ */ w(o, {
		title: "No entry codes",
		description: "Mint a code to hand out — anyone who opens its chat link enters the gated route."
	}) : /* @__PURE__ */ w(l, {
		label: "Entry codes",
		children: /* @__PURE__ */ T(_, { children: [/* @__PURE__ */ w(h, { children: /* @__PURE__ */ T(g, { children: [
			/* @__PURE__ */ w(m, { children: "Label" }),
			/* @__PURE__ */ w(m, { children: "Created" }),
			/* @__PURE__ */ w(m, { children: "Expires" }),
			/* @__PURE__ */ w(m, { "aria-label": "Actions" })
		] }) }), /* @__PURE__ */ w(f, { children: t.map((t) => /* @__PURE__ */ T(g, { children: [
			/* @__PURE__ */ w(p, { children: t.label ?? "—" }),
			/* @__PURE__ */ w(p, { children: Et(t.created_at) }),
			/* @__PURE__ */ w(p, { children: Dt(t.expires_at) }),
			/* @__PURE__ */ w(p, {
				style: { textAlign: "right" },
				children: /* @__PURE__ */ w(e, {
					variant: "ghost",
					"aria-label": `Revoke entry code ${t.label ?? t.code_id}`,
					onClick: () => {
						n(t.code_id);
					},
					children: "Revoke"
				})
			})
		] }, t.code_id)) })] })
	});
}
//#endregion
//#region studio-src/compose-chat-url.ts
function kt(e, t, n, r) {
	return `${(e === "" ? window.location.origin : e).replace(/\/+$/, "")}${t}/chat/${encodeURIComponent(n)}?tai_entry=${encodeURIComponent(r)}`;
}
//#endregion
//#region node_modules/.pnpm/zod@4.6.5/node_modules/zod/v4/core/util.js
function At(e) {
	let t = Object.values(e).filter((e) => typeof e == "number");
	return Object.entries(e).filter(([e, n]) => t.indexOf(+e) === -1).map(([e, t]) => t);
}
function jt(e, t = "|") {
	return e.map((e) => Zt(e)).join(t);
}
function Mt(e, t) {
	return typeof t == "bigint" ? t.toString() : t;
}
var Nt = class {
	constructor(e) {
		this._getter = e, this._value = void 0;
	}
	get value() {
		let e = this._getter;
		return e !== void 0 && (this._value = e(), this._getter = void 0), this._value;
	}
};
function Pt(e) {
	return new Nt(e);
}
function Ft(e) {
	return e == null;
}
function It(e) {
	let t = +!!e.startsWith("^"), n = e.endsWith("$") ? e.length - 1 : e.length;
	return e.slice(t, n);
}
function j(e, t, n) {
	Object.defineProperty(e, t, {
		value: n,
		writable: !0,
		enumerable: !0,
		configurable: !0
	});
}
function Lt(e) {
	let t = Object.getOwnPropertyDescriptor(e, "shape");
	return t?.get ? t.get.raw : t?.value;
}
function M(e) {
	return Lt(e._zod.def) ?? e._zod.def.shape;
}
function Rt(e, t, n) {
	Object.defineProperty(e, t, {
		get() {
			let e = n();
			return j(this, t, e), e;
		},
		enumerable: !0,
		configurable: !0
	});
}
function zt(e, t, n) {
	t in e ? j(e, t, n) : e[t] = n;
}
function Bt(e, t, n, r) {
	let i = M(t);
	for (let a of n) {
		let n = Object.getOwnPropertyDescriptor(i, a);
		n.enumerable && (n.get ? Rt(e, a, () => {
			let e = t._zod.def.shape[a];
			return r ? r(e, a) : e;
		}) : zt(e, a, r ? r(n.value, a) : n.value));
	}
}
function Vt(e, t) {
	for (let n of Reflect.ownKeys(t)) {
		let r = Object.getOwnPropertyDescriptor(t, n);
		r.enumerable && (r.get ? Rt(e, n, () => t[n]) : zt(e, n, r.value));
	}
}
function N(...e) {
	let t = {};
	for (let n of e) {
		let e = Object.getOwnPropertyDescriptors(n);
		Object.assign(t, e);
	}
	return Object.defineProperties({}, t);
}
function Ht(e) {
	return JSON.stringify(e);
}
function Ut(e) {
	return e.toLowerCase().trim().replace(/[^\w\s-]/g, "").replace(/[\s_-]+/g, "-").replace(/^-+|-+$/g, "");
}
var Wt = "captureStackTrace" in Error ? Error.captureStackTrace : (...e) => {};
function Gt(e) {
	return typeof e == "object" && !!e && !Array.isArray(e);
}
var Kt = /* @__PURE__*/ Pt(() => {
	if (H.jitless || typeof navigator < "u" && navigator?.userAgent?.includes("Cloudflare")) return !1;
	try {
		return Function(""), !0;
	} catch {
		return !1;
	}
});
function qt(e) {
	if (Gt(e) === !1) return !1;
	let t = e.constructor;
	if (t === void 0 || typeof t != "function") return !0;
	let n = t.prototype;
	return Gt(n) !== !1 && Object.prototype.hasOwnProperty.call(n, "isPrototypeOf") !== !1;
}
function Jt(e) {
	return qt(e) ? { ...e } : Array.isArray(e) ? [...e] : e instanceof Map ? new Map(e) : e instanceof Set ? new Set(e) : e;
}
var Yt = /* @__PURE__*/ new Set([
	"string",
	"number",
	"symbol"
]);
function Xt(e) {
	return e.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
function P(e, t, n) {
	let r = new e._zod.constr(t ?? e._zod.def);
	return (!t || n?.parent) && (r._zod.parent = e), r;
}
function F(e) {
	let t = e;
	if (!t) return {};
	if (typeof t == "string") return { error: () => t };
	if (t?.message !== void 0) {
		if (t?.error !== void 0) throw Error("Cannot specify both `message` and `error` params");
		t.error = t.message;
	}
	return delete t.message, typeof t.error == "string" ? {
		...t,
		error: () => t.error
	} : t;
}
function Zt(e) {
	return typeof e == "bigint" ? e.toString() + "n" : typeof e == "string" ? `"${e}"` : `${e}`;
}
function Qt(e) {
	return Object.keys(e).filter((t) => e[t]._zod.optin !== void 0 && e[t]._zod.optout === "optional");
}
var $t = {
	safeint: [-(2 ** 53 - 1), 2 ** 53 - 1],
	int32: [-2147483648, 2147483647],
	uint32: [0, 4294967295],
	float32: [-34028234663852886e22, 34028234663852886e22],
	float64: [-Number.MAX_VALUE, Number.MAX_VALUE]
}, en = {
	int64: [/* @__PURE__*/ BigInt("-9223372036854775808"), /* @__PURE__*/ BigInt("9223372036854775807")],
	uint64: [/* @__PURE__*/ BigInt(0), /* @__PURE__*/ BigInt("18446744073709551615")]
};
function tn(e, t) {
	let n = e._zod.def, r = n.checks;
	if (r && r.length > 0) throw Error(".pick() cannot be used on object schemas containing refinements");
	let i = {};
	return Bt(i, e, nn(e, t)), P(e, N(n, {
		shape: i,
		checks: []
	}));
}
function nn(e, t) {
	let n = M(e), r = [];
	for (let e of Reflect.ownKeys(t)) {
		if (!Object.getOwnPropertyDescriptor(n, e)?.enumerable) throw Error(`Unrecognized key: "${String(e)}"`);
		t[e] && r.push(e);
	}
	return r;
}
function rn(e, t) {
	let n = e._zod.def, r = n.checks;
	if (r && r.length > 0) throw Error(".omit() cannot be used on object schemas containing refinements");
	let i = new Set(nn(e, t)), a = {};
	return Bt(a, e, Reflect.ownKeys(M(e)).filter((e) => !i.has(e))), P(e, N(n, {
		shape: a,
		checks: []
	}));
}
function an(e, t) {
	if (!qt(t)) throw Error("Invalid input to extend: expected a plain object");
	let n = e._zod.def.checks;
	if (n && n.length > 0) {
		let n = M(e);
		for (let e of Reflect.ownKeys(t)) if (Object.getOwnPropertyDescriptor(n, e) !== void 0) throw Error("Cannot overwrite keys on object schemas containing refinements. Use `.safeExtend()` instead.");
	}
	return P(e, N(e._zod.def, { shape: on(e, t) }));
}
function on(e, t) {
	let n = {};
	return Bt(n, e, Reflect.ownKeys(M(e))), Vt(n, t), n;
}
function sn(e, t) {
	if (!qt(t)) throw Error("Invalid input to safeExtend: expected a plain object");
	return P(e, N(e._zod.def, { shape: on(e, t) }));
}
function cn(e, t) {
	if (!t?._zod?.def) throw Error("Invalid input to merge: expected an object schema. To merge a plain shape, use `.extend()`.");
	if (e._zod.def.checks?.length) throw Error(".merge() cannot be used on object schemas containing refinements. Use .safeExtend() instead.");
	let n = {};
	return Bt(n, e, Reflect.ownKeys(M(e))), Bt(n, t, Reflect.ownKeys(M(t))), P(e, N(e._zod.def, {
		shape: n,
		get catchall() {
			return t._zod.def.catchall;
		},
		checks: t._zod.def.checks ?? []
	}));
}
function ln(e, t, n, r = "partial") {
	let i = t._zod.def.checks;
	if (i && i.length > 0) throw Error(`.${r}() cannot be used on object schemas containing refinements`);
	let a = n ? new Set(nn(t, n)) : void 0, o = {};
	return Bt(o, t, Reflect.ownKeys(M(t)), e && ((t, n) => a && !a.has(n) ? t : new e({
		type: "optional",
		innerType: t
	}))), P(t, N(t._zod.def, {
		shape: o,
		checks: []
	}));
}
function un(e, t, n) {
	let r = n ? new Set(nn(t, n)) : void 0, i = {};
	return Bt(i, t, Reflect.ownKeys(M(t)), (t, n) => r && !r.has(n) ? t : new e({
		type: "nonoptional",
		innerType: t
	})), P(t, N(t._zod.def, { shape: i }));
}
function I(e, t = 0) {
	if (e.aborted === !0) return !0;
	for (let n = t; n < e.issues.length; n++) if (e.issues[n]?.continue !== !0) return !0;
	return !1;
}
function dn(e, t = 0) {
	if (e.aborted === !0) return !0;
	for (let n = t; n < e.issues.length; n++) if (e.issues[n]?.continue === !1) return !0;
	return !1;
}
function fn(e, t) {
	return t.map((t) => {
		var n;
		return (n = t).path ?? (n.path = []), t.path.unshift(e), t;
	});
}
function pn(e) {
	return typeof e == "string" ? e : e?.message;
}
function mn(e, t, n) {
	var r;
	for (let i = t; i < e.length; i++) (r = e[i]).schema ?? (r.schema = n);
}
function L(e, t, n) {
	var r;
	let i = e.inst?._zod?.traits;
	i?.has("$ZodType") && (i.has("$ZodCheck") ? (r = e).schema ?? (r.schema = e.inst) : e.schema = e.inst);
	let a = e.schema === e.inst ? void 0 : e.schema?._zod.def?.error, o = e.message ? e.message : pn(e.inst?._zod.def?.error?.(e)) ?? pn(a?.(e)) ?? pn(t?.error?.(e)) ?? pn(n.customError?.(e)) ?? pn(n.localeError?.(e)) ?? "Invalid input", s = {};
	for (let t of Object.keys(e)) t !== "inst" && t !== "schema" && t !== "continue" && t !== "input" && t !== "__proto__" && (s[t] = e[t]);
	return s.path ??= [], s.message = o, t?.reportInput && (s.input = e.input), s;
}
var hn = /[\uD800-\uDBFF]/;
function gn(e) {
	let t = e.length;
	if (!hn.test(e)) return t;
	let n = t;
	for (let r = 0; r < t - 1; r++) (e.charCodeAt(r) & 64512) == 55296 && (e.charCodeAt(r + 1) & 64512) == 56320 && (n--, r++);
	return n;
}
function _n(e) {
	return Array.isArray(e) ? "array" : typeof e == "string" ? "string" : "unknown";
}
function vn(e) {
	let t = typeof e;
	switch (t) {
		case "number": return Number.isNaN(e) ? "nan" : "number";
		case "object": {
			if (e === null) return "null";
			if (Array.isArray(e)) return "array";
			let t = e;
			if (t && Object.getPrototypeOf(t) !== Object.prototype && "constructor" in t && t.constructor) return t.constructor.name;
		}
	}
	return t;
}
function yn(...e) {
	let [t, n, r] = e;
	return typeof t == "string" ? {
		message: t,
		code: "custom",
		input: n,
		inst: r
	} : { ...t };
}
function bn(e, t) {
	for (let n in t) {
		let r = Object.getOwnPropertyDescriptor(t, n);
		r.get ? Object.defineProperty(e, n, {
			...r,
			enumerable: !1
		}) : Cn(e, n, r.value);
	}
}
function R(e, t, n, r = !0) {
	return Object.defineProperty(e, t, {
		configurable: !0,
		writable: !0,
		enumerable: r,
		value: n
	}), n;
}
function xn(e, t, n) {
	return R(e, t, n, !1);
}
function Sn(e, t) {
	for (let n in e) {
		let r = e[n];
		Object.defineProperty(t, n, {
			configurable: !0,
			enumerable: !0,
			get() {
				return R(this, n, r(this));
			},
			set(e) {
				R(this, n, e);
			}
		});
	}
	return t;
}
function Cn(e, t, n) {
	Object.defineProperty(e, t, {
		configurable: !0,
		get() {
			return this == null ? n : R(this, t, n.bind(this));
		},
		set(e) {
			R(this, t, e);
		}
	});
}
function wn(e, t) {
	let n = Object.getPrototypeOf(e);
	return t in n ? void 0 : n;
}
var Tn, En = !1, Dn = {
	configurable: !0,
	get() {
		En = !0;
	}
};
function z(e, t, n) {
	let r = Object.getPrototypeOf(e._zod);
	t in r && Tn !== e._zod ? Tn = void 0 : (Tn = e._zod, Object.defineProperty(r, t, {
		configurable: !0,
		get() {
			Object.defineProperty(this, t, Dn);
			let e = En;
			En = !1;
			try {
				let r = n(this);
				return En ? delete this[t] : Object.defineProperty(this, t, {
					configurable: !0,
					writable: !0,
					value: r
				}), En ||= e, r;
			} catch (n) {
				throw delete this[t], En ||= e, n;
			}
		},
		set(e) {
			Object.defineProperty(this, t, {
				configurable: !0,
				writable: !0,
				value: e
			});
		}
	}));
}
function On(e, t, n, r) {
	let i = wn(e, t);
	i && Object.defineProperty(i, t, {
		configurable: !0,
		get() {
			let e = {
				configurable: !0,
				writable: !0,
				enumerable: r,
				value: void 0
			};
			return Object.defineProperty(this, t, e), e.value = n(this), Object.defineProperty(this, t, e), e.value;
		},
		set(e) {
			Object.defineProperty(this, t, {
				configurable: !0,
				writable: !0,
				enumerable: r,
				value: e
			});
		}
	});
}
var kn = "~constantCatch";
function An(e) {
	let t = () => e;
	return t[kn] = !0, t;
}
//#endregion
//#region node_modules/.pnpm/zod@4.6.5/node_modules/zod/v4/core/core.js
var jn, Mn = {
	value: void 0,
	enumerable: !1
}, Nn = "captureStackTrace" in Error ? Error : null;
function Pn(e) {
	let t = Nn;
	if (t) {
		let n = t.stackTraceLimit;
		if (typeof n == "number") {
			try {
				t.stackTraceLimit = 0;
			} catch {
				return Nn = null, new e();
			}
			try {
				return new e();
			} finally {
				t.stackTraceLimit = n;
			}
		}
	}
	return new e();
}
function B(e, t, n, r) {
	let i = {};
	function a(e) {
		this.def = e, this.constr = d, this.traits = /* @__PURE__ */ new Set();
	}
	a.prototype = i;
	let o = n, s = o && /* @__PURE__ */ new WeakSet();
	function c(n, r) {
		if (!n._zod) {
			Mn.value = new a(r);
			try {
				Object.defineProperty(n, "_zod", Mn);
			} finally {
				Mn.value = void 0;
			}
		} else if (n._zod.traits.has(e)) return;
		if (n._zod.traits.add(e), t(n, r), s) {
			let e = Object.getPrototypeOf(n), t = n._zod.constr.prototype, r = e;
			for (; r && r !== t;) r = Object.getPrototypeOf(r);
			let i = r ?? e;
			s.has(i) || (s.add(i), bn(i, o));
		}
		let i = d.prototype;
		for (let e in i) Object.prototype.hasOwnProperty.call(i, e) && (e in n || (n[e] = i[e].bind(n)));
	}
	let l = r?.Parent ?? Object;
	class u extends l {}
	Object.defineProperty(u, "name", { value: e });
	function d(e) {
		let t = r?.Parent ? Pn(u) : this;
		c(t, e);
		let n = t._zod.deferred;
		if (n) {
			for (let e of n) e();
			t._zod.deferred = void 0;
		}
		let i = globalThis.__zod_globalConfig?.postProcessor;
		return i && i(t), t;
	}
	return Object.defineProperty(d, "init", { value: c }), Object.defineProperty(d, Symbol.hasInstance, { value: (t) => r?.Parent && t instanceof r.Parent ? !0 : t?._zod?.traits?.has(e) }), Object.defineProperty(d, "name", { value: e }), d;
}
var V = class extends Error {
	constructor() {
		super("Encountered Promise during synchronous parse. Use .parseAsync() instead.");
	}
}, Fn = class extends Error {
	constructor(e) {
		super(`Encountered unidirectional transform during encode: ${e}`), this.name = "ZodEncodeError";
	}
};
(jn = globalThis).__zod_globalConfig ?? (jn.__zod_globalConfig = {});
var H = globalThis.__zod_globalConfig;
function U(e) {
	return e && Object.assign(H, e), H;
}
//#endregion
//#region node_modules/.pnpm/zod@4.6.5/node_modules/zod/v4/core/errors.js
function In() {
	let e = this._zod;
	return e.message ??= JSON.stringify(e.def, Mt, 2), e.message;
}
function Ln(e) {
	this._zod.message = e;
}
var Rn = {
	get: In,
	set: Ln,
	enumerable: !0,
	configurable: !0
}, zn = {
	value: void 0,
	enumerable: !1
}, Bn = /* @__PURE__ */ new WeakSet([Object.prototype, Error.prototype]), Vn = (e, t) => {
	e.name = "$ZodError", zn.value = t, Object.defineProperty(e, "issues", zn), zn.value = void 0, Object.defineProperty(e, "message", Rn);
	let n = Object.getPrototypeOf(e);
	Bn.has(n) || (Bn.add(n), Object.defineProperty(n, "toString", {
		configurable: !0,
		enumerable: !1,
		get() {
			let e = () => this.message;
			return Object.defineProperty(this, "toString", {
				value: e,
				configurable: !0,
				writable: !0
			}), e;
		},
		set(e) {
			Object.defineProperty(this, "toString", {
				value: e,
				configurable: !0,
				writable: !0
			});
		}
	}));
}, Hn = B("$ZodError", Vn);
B("$ZodError", Vn, void 0, { Parent: Error });
function Un(e, t, n) {
	return Object.prototype.hasOwnProperty.call(e, t) || (t === "__proto__" ? Object.defineProperty(e, t, {
		value: n(),
		writable: !0,
		enumerable: !0,
		configurable: !0
	}) : e[t] = n()), e[t];
}
function Wn(e, t = (e) => e.message) {
	let n = {}, r = [];
	for (let i of e.issues) i.path.length > 0 ? Un(n, i.path[0], () => []).push(t(i)) : r.push(t(i));
	return {
		formErrors: r,
		fieldErrors: n
	};
}
function Gn(e, t = (e) => e.message) {
	let n = { _errors: [] }, r = (e, i = []) => {
		for (let a of e.issues) if (a.code === "invalid_union" && a.errors.length) a.errors.map((e) => r({ issues: e }, [...i, ...a.path]));
		else if (a.code === "invalid_key") r({ issues: a.issues }, [...i, ...a.path]);
		else if (a.code === "invalid_element") r({ issues: a.issues }, [...i, ...a.path]);
		else {
			let e = [...i, ...a.path];
			if (e.length === 0) n._errors.push(t(a));
			else {
				let r = n, i = 0;
				for (; i < e.length;) {
					let n = e[i], o = i === e.length - 1;
					if (n === "_errors") {
						o && r._errors.push(t(a)), i++;
						continue;
					}
					Object.prototype.hasOwnProperty.call(r, n) || Object.defineProperty(r, n, {
						value: { _errors: [] },
						enumerable: !0,
						writable: !0,
						configurable: !0
					});
					let s = r[n];
					o && s._errors.push(t(a)), r = s, i++;
				}
			}
		}
	};
	return r(e), n;
}
//#endregion
//#region node_modules/.pnpm/zod@4.6.5/node_modules/zod/v4/core/parse.js
function Kn(e, t) {
	return {
		callee: t?.callee ?? e,
		Err: t?.Err
	};
}
var qn = (e) => {
	let t = (n, r, i, a) => {
		let o = i ? {
			...i,
			async: !1
		} : { async: !1 }, s = n._zod.run({
			value: r,
			issues: []
		}, o);
		if (s instanceof Promise) throw new V();
		if (s.issues.length) {
			let n = new ((a?.Err) ?? e)(s.issues.map((e) => L(e, o, U())));
			throw Wt(n, a?.callee ?? t), n;
		}
		return s.value;
	};
	return t;
}, Jn = (e) => {
	let t = async (n, r, i, a) => {
		let o = i ? {
			...i,
			async: !0
		} : { async: !0 }, s = n._zod.run({
			value: r,
			issues: []
		}, o);
		if (s instanceof Promise && (s = await s), s.issues.length) {
			let n = new ((a?.Err) ?? e)(s.issues.map((e) => L(e, o, U())));
			throw Wt(n, a?.callee ?? t), n;
		}
		return s.value;
	};
	return t;
}, Yn = (e) => (t, n, r) => {
	let i = r ? {
		...r,
		async: !1
	} : { async: !1 }, a = t._zod.run({
		value: n,
		issues: []
	}, i);
	if (a instanceof Promise) throw new V();
	return a.issues.length ? Xn(e, a.issues, i) : {
		success: !0,
		data: a.value
	};
};
function Xn(e, t, n) {
	let r;
	return {
		success: !1,
		get error() {
			return r || (r = new e(t.map((e) => L(e, n, U()))), t = void 0, n = void 0), r;
		},
		set error(e) {
			r = e, t = void 0, n = void 0;
		}
	};
}
var Zn = (e) => async (t, n, r) => {
	let i = r ? {
		...r,
		async: !0
	} : { async: !0 }, a = t._zod.run({
		value: n,
		issues: []
	}, i);
	return a instanceof Promise && (a = await a), a.issues.length ? Xn(e, a.issues, i) : {
		success: !0,
		data: a.value
	};
}, Qn = /* @__PURE__ */ Symbol.for("zod.compile.invalid"), $n = /* @__PURE__ */ Symbol.for("zod.compile.fallback"), er = ((e, t, n) => {
	let r = e._zod.bag.validator;
	if (r !== void 0) {
		if (r(t) !== Qn) return !0;
		if (r.definite === !0 && n === void 0) return !1;
	}
	return tr(e, t, n);
});
function tr(e, t, n) {
	let r = n ? {
		...n,
		async: !1,
		abortEarly: !0
	} : {
		async: !1,
		abortEarly: !0
	}, i = e._zod.bag.fallbackRun, a;
	if (i ? (r[$n] = !0, a = i({
		value: t,
		issues: []
	}, r)) : a = e._zod.run({
		value: t,
		issues: []
	}, r), a instanceof Promise) throw new V();
	return a.issues.length === 0;
}
var nr = async (e, t, n) => {
	let r = n ? {
		...n,
		async: !0,
		abortEarly: !0
	} : {
		async: !0,
		abortEarly: !0
	}, i = e._zod.run({
		value: t,
		issues: []
	}, r);
	return i instanceof Promise && (i = await i), i.issues.length === 0;
}, rr = (e) => {
	let t = qn(e), n = (e, r, i, a) => {
		let o = i ? {
			...i,
			direction: "backward"
		} : { direction: "backward" };
		return t(e, r, o, Kn(n, a));
	};
	return n;
}, ir = (e) => {
	let t = qn(e), n = (e, r, i, a) => t(e, r, i, Kn(n, a));
	return n;
}, ar = (e) => {
	let t = Jn(e), n = async (e, r, i, a) => {
		let o = i ? {
			...i,
			direction: "backward"
		} : { direction: "backward" };
		return await t(e, r, o, Kn(n, a));
	};
	return n;
}, or = (e) => {
	let t = Jn(e), n = async (e, r, i, a) => await t(e, r, i, Kn(n, a));
	return n;
}, sr = (e) => (t, n, r) => {
	let i = r ? {
		...r,
		direction: "backward"
	} : { direction: "backward" };
	return Yn(e)(t, n, i);
}, cr = (e) => (t, n, r) => Yn(e)(t, n, r), lr = (e) => async (t, n, r) => {
	let i = r ? {
		...r,
		direction: "backward"
	} : { direction: "backward" };
	return Zn(e)(t, n, i);
}, ur = (e) => async (t, n, r) => Zn(e)(t, n, r), dr = /^[cC][0-9a-z]{6,}$/, fr = /^[0-9a-z]+$/, pr = /^[0-7][0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{25}$/, mr = /^[0-9a-vA-V]{20}$/, hr = /^[A-Za-z0-9]{27}$/, gr = /^[a-zA-Z0-9_-]{21}$/;
function _r(e) {
	return RegExp(`^[a-zA-Z0-9_-]{${e}}$`);
}
var vr = /^P(?:(\d+W)|(?!.*W)(?=\d|T\d)(\d+Y)?(\d+M)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+([.,]\d+)?S)?)?)$/, yr = /^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$/, br = (e) => e ? RegExp(`^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-${e}[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})$`) : /^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff)$/, xr = /^(?:[A-Za-z0-9_'+\-]+\.)*[A-Za-z0-9_'+\-]*[A-Za-z0-9_+-]@(?:[A-Za-z0-9][A-Za-z0-9\-]*\.)+[A-Za-z]{2,}$/, Sr = "^(?=[\\s\\S]*[\\p{Extended_Pictographic}\\p{Regional_Indicator}\\u20E3])[\\p{Extended_Pictographic}\\p{Emoji_Component}]+$";
function Cr() {
	return new RegExp(Sr, "u");
}
var wr = /^(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])$/, Tr = /^(([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,7}:|([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|:((:[0-9a-fA-F]{1,4}){1,7}|:))$/, Er = /^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])\/([0-9]|[1-2][0-9]|3[0-2])$/, Dr = /^(([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,7}:|([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|:((:[0-9a-fA-F]{1,4}){1,7}|:))\/(12[0-8]|1[01][0-9]|[1-9]?[0-9])$/, Or = /^$|^(?:[0-9a-zA-Z+/]{4})*(?:(?:[0-9a-zA-Z+/]{2}==)|(?:[0-9a-zA-Z+/]{3}=))?$/, kr = /^(?:[A-Za-z0-9_-]{4})*(?:[A-Za-z0-9_-]{2,3})?$/, Ar = /^https?$/, jr = /^\+[1-9]\d{6,14}$/, Mr = "(?:(?:\\d\\d[2468][048]|\\d\\d[13579][26]|\\d\\d0[48]|[02468][048]00|[13579][26]00)-02-29|\\d{4}-(?:(?:0[13578]|1[02])-(?:0[1-9]|[12]\\d|3[01])|(?:0[469]|11)-(?:0[1-9]|[12]\\d|30)|(?:02)-(?:0[1-9]|1\\d|2[0-8])))";
function Nr(e) {
	return RegExp(`^${e}$`);
}
var Pr = /*@__PURE__*/ Nr(Mr);
function Fr(e) {
	let t = "(?:[01]\\d|2[0-3]):[0-5]\\d";
	return typeof e.precision == "number" ? e.precision === -1 ? `${t}` : e.precision === 0 ? `${t}:[0-5]\\d` : `${t}:[0-5]\\d\\.\\d{${e.precision}}` : e.seconds ? `${t}:[0-5]\\d(?:\\.\\d+)?` : `${t}(?::[0-5]\\d(?:\\.\\d+)?)?`;
}
function Ir(e) {
	return RegExp(`^${Fr(e)}$`);
}
function Lr(e) {
	let t = ["Z"];
	e.offset && t.push("([+-](?:[01]\\d|2[0-3]):[0-5]\\d)");
	let n = `${Fr({
		precision: e.precision,
		seconds: !0
	})}(?:${t.join("|")})`, r = e.local ? `${n}|${Fr({ precision: e.precision })}` : n;
	return RegExp(`^${Mr}T(?:${r})$`);
}
var Rr = /^[\s\S]{0,}$/, zr = /^-?\d+$/, Br = /^-?\d+(?:\.\d+)?$/, Vr = /^(?:true|false)$/i, Hr = /^[^A-Z]*$/, Ur = /^[^a-z]*$/, W = /*@__PURE__*/ B("$ZodCheck", (e, t) => {
	var n;
	e._zod ??= {}, e._zod.def = t, (n = e._zod).onattach ?? (n.onattach = []);
}), Wr = (e) => {
	let t = e.value;
	return !Ft(t) && t.length !== void 0;
}, Gr = /*@__PURE__*/ B("$ZodCheckMaxLength", (e, t) => {
	var n;
	W.init(e, t), (n = e._zod.def).when ?? (n.when = Wr), e._zod.check = (n) => {
		let r = n.value, i = r.length;
		if ((typeof r == "string" && i > t.maximum ? gn(r) : i) <= t.maximum) return;
		let a = _n(r);
		n.issues.push({
			origin: a,
			code: "too_big",
			maximum: t.maximum,
			inclusive: !0,
			input: r,
			inst: e,
			continue: !t.abort
		});
	};
}), Kr = /*@__PURE__*/ B("$ZodCheckMinLength", (e, t) => {
	var n;
	W.init(e, t), (n = e._zod.def).when ?? (n.when = Wr), e._zod.check = (n) => {
		let r = n.value, i = r.length;
		if ((typeof r == "string" && i >= t.minimum && i < t.minimum * 2 ? gn(r) : i) >= t.minimum) return;
		let a = _n(r);
		n.issues.push({
			origin: a,
			code: "too_small",
			minimum: t.minimum,
			inclusive: !0,
			input: r,
			inst: e,
			continue: !t.abort
		});
	};
}), qr = /*@__PURE__*/ B("$ZodCheckLengthEquals", (e, t) => {
	var n;
	W.init(e, t), (n = e._zod.def).when ?? (n.when = Wr), e._zod.check = (n) => {
		let r = n.value, i = r.length, a = typeof r == "string" && i >= t.length && i <= t.length * 2 ? gn(r) : i;
		if (a === t.length) return;
		let o = _n(r), s = a > t.length;
		n.issues.push({
			origin: o,
			...s ? {
				code: "too_big",
				maximum: t.length
			} : {
				code: "too_small",
				minimum: t.length
			},
			inclusive: !0,
			exact: !0,
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), Jr = /*@__PURE__*/ B("$ZodCheckStringFormat", (e, t) => {
	var n, r;
	W.init(e, t), t.pattern ? (n = e._zod).check ?? (n.check = (n) => {
		t.pattern.lastIndex = 0, !t.pattern.test(n.value) && n.issues.push({
			origin: "string",
			code: "invalid_format",
			format: t.format,
			input: n.value,
			...t.pattern ? { pattern: t.pattern.toString() } : {},
			inst: e,
			continue: !t.abort
		});
	}) : (r = e._zod).check ?? (r.check = () => {});
}), Yr = /*@__PURE__*/ B("$ZodCheckRegex", (e, t) => {
	Jr.init(e, t), e._zod.check = (n) => {
		t.pattern.lastIndex = 0, !t.pattern.test(n.value) && n.issues.push({
			origin: "string",
			code: "invalid_format",
			format: "regex",
			input: n.value,
			pattern: t.pattern.toString(),
			inst: e,
			continue: !t.abort
		});
	};
}), Xr = /*@__PURE__*/ B("$ZodCheckLowerCase", (e, t) => {
	t.pattern ??= Hr, Jr.init(e, t);
}), Zr = /*@__PURE__*/ B("$ZodCheckUpperCase", (e, t) => {
	t.pattern ??= Ur, Jr.init(e, t);
}), Qr = /*@__PURE__*/ B("$ZodCheckIncludes", (e, t) => {
	W.init(e, t);
	let n = Xt(t.includes);
	t.pattern = new RegExp(typeof t.position == "number" ? `^.{${t.position},}${n}` : n), e._zod.check = (n) => {
		n.value.includes(t.includes, t.position) || n.issues.push({
			origin: "string",
			code: "invalid_format",
			format: "includes",
			includes: t.includes,
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), $r = /*@__PURE__*/ B("$ZodCheckStartsWith", (e, t) => {
	W.init(e, t);
	let n = RegExp(`^${Xt(t.prefix)}.*`);
	t.pattern ??= n, e._zod.check = (n) => {
		n.value.startsWith(t.prefix) || n.issues.push({
			origin: "string",
			code: "invalid_format",
			format: "starts_with",
			prefix: t.prefix,
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), ei = /*@__PURE__*/ B("$ZodCheckEndsWith", (e, t) => {
	W.init(e, t);
	let n = RegExp(`.*${Xt(t.suffix)}$`);
	t.pattern ??= n, e._zod.check = (n) => {
		n.value.endsWith(t.suffix) || n.issues.push({
			origin: "string",
			code: "invalid_format",
			format: "ends_with",
			suffix: t.suffix,
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), ti = /*@__PURE__*/ B("$ZodCheckOverwrite", (e, t) => {
	W.init(e, t), e._zod.check = (e) => {
		e.value = t.tx(e.value);
	};
}), ni = class {
	constructor(e = [], t = {}) {
		this.content = [], this.indent = 0, this.args = e, this.closed = t;
	}
	indented(e) {
		this.indent += 1;
		try {
			e(this);
		} finally {
			--this.indent;
		}
	}
	write(e) {
		if (typeof e == "function") {
			e(this, { execution: "sync" }), e(this, { execution: "async" });
			return;
		}
		let t = e.split("\n").filter((e) => e), n = Math.min(...t.map((e) => e.length - e.trimStart().length)), r = t.map((e) => e.slice(n)).map((e) => " ".repeat(this.indent * 2) + e);
		for (let e of r) this.content.push(e);
	}
	compile() {
		let e = Function, t = this?.content ?? [""];
		return new e(...Object.keys(this.closed), `return function (${this.args.join(", ")}) {\n${t.join("\n")}\n};`)(...Object.values(this.closed));
	}
}, ri = {
	major: 4,
	minor: 6,
	patch: 5
}, G = /*@__PURE__*/ B("$ZodType", (e, t) => {
	var n;
	e ??= {}, e._zod.def = t, e._zod.bag = e._zod.bag || {}, e._zod.version = ri;
	let r = e._zod.def.checks, i = e._zod.traits.has("$ZodCheck") ? [e, ...r ?? []] : r?.length ? [...r] : [];
	for (let t of i) for (let n of t._zod.onattach) n(e);
	if (i.length === 0) (n = e._zod).deferred ?? (n.deferred = []), e._zod.deferred?.push(() => {
		e._zod.run = e._zod.parse;
	});
	else {
		let t = (t, n, r) => {
			if (t.memo) return t;
			let i = I(t), a;
			for (let o of n) {
				if (o._zod.def.when) {
					if (dn(t) || !o._zod.def.when(t)) continue;
				} else if (i) continue;
				let n = t.issues.length, s = o._zod.check(t);
				if (s instanceof Promise && r?.async === !1) throw new V();
				if (a || s instanceof Promise) a = (a ?? Promise.resolve()).then(async () => {
					await s, t.issues.length !== n && (mn(t.issues, n, e), i ||= I(t, n));
				});
				else {
					if (t.issues.length === n) continue;
					mn(t.issues, n, e), i ||= I(t, n);
				}
			}
			return a ? a.then(() => t) : t;
		}, n = (n, r, a) => {
			if (I(n)) return n.aborted = !0, n;
			let o = t(r, i, a);
			if (o instanceof Promise) {
				if (a.async === !1) throw new V();
				return o.then((t) => e._zod.parse(t, a));
			}
			return e._zod.parse(o, a);
		};
		e._zod.run = (r, a) => {
			if (a.skipChecks) return e._zod.parse(r, a);
			if (a.direction === "backward") {
				let t = e._zod.parse({
					value: r.value,
					issues: []
				}, {
					...a,
					skipChecks: !0
				});
				return t instanceof Promise ? t.then((e) => n(e, r, a)) : n(t, r, a);
			}
			let o = e._zod.parse(r, a);
			if (o instanceof Promise) {
				if (a.async === !1) throw new V();
				return o.then((e) => t(e, i, a));
			}
			return t(o, i, a);
		};
	}
}, {
	get "~standard"() {
		return xn(this, "~standard", oi(this));
	},
	set "~standard"(e) {
		R(this, "~standard", e);
	}
}), ii = (e, t) => e.issues.length ? { issues: e.issues.map((e) => L(e, t, U())) } : { value: e.value };
async function ai(e, t) {
	let n = { async: !0 };
	return ii(await e._zod.run({
		value: t,
		issues: []
	}, n), n);
}
function oi(e) {
	return {
		validate: (t) => {
			let n = { async: !1 };
			try {
				let r = e._zod.run({
					value: t,
					issues: []
				}, n);
				if (!(r instanceof Promise)) return ii(r, n);
			} catch {}
			return ai(e, t);
		},
		vendor: "zod",
		version: 1
	};
}
var si = /*@__PURE__*/ B("$ZodString", (e, t) => {
	G.init(e, t), e._zod.pattern = t.pattern ?? Rr, e._zod.parse = (n, r) => {
		if (t.coerce) try {
			n.value = String(n.value);
		} catch {}
		return typeof n.value == "string" || n.issues.push({
			expected: "string",
			code: "invalid_type",
			input: n.value,
			inst: e
		}), n;
	};
}), K = /*@__PURE__*/ B("$ZodStringFormat", (e, t) => {
	Jr.init(e, t), si.init(e, t);
}), ci = /*@__PURE__*/ B("$ZodGUID", (e, t) => {
	t.pattern ??= yr, K.init(e, t);
}), li = /*@__PURE__*/ B("$ZodUUID", (e, t) => {
	if (t.version) {
		let e = {
			v1: 1,
			v2: 2,
			v3: 3,
			v4: 4,
			v5: 5,
			v6: 6,
			v7: 7,
			v8: 8
		}[t.version];
		if (e === void 0) throw Error(`Invalid UUID version: "${t.version}"`);
		t.pattern ??= br(e);
	} else t.pattern ??= br();
	K.init(e, t);
}), ui = /*@__PURE__*/ B("$ZodEmail", (e, t) => {
	t.pattern ??= xr, K.init(e, t);
});
function di(e) {
	try {
		return typeof URL < "u" && typeof URL.canParse == "function" ? URL.canParse(e) : (new URL(e), !0);
	} catch {
		return !1;
	}
}
function fi(e, t) {
	return !("normalize" in t) && !("hostname" in t) && !("protocol" in t) ? di(e) || 2 : pi(e, t);
}
function pi(e, t) {
	if (!t.normalize && t.protocol?.source === Ar.source && !/^https?:\/\//i.test(e)) return 1;
	try {
		if (typeof URL < "u") {
			let t = URL;
			if (typeof t.parse == "function") return t.parse(e) ?? 2;
		}
		return new URL(e);
	} catch {
		return 2;
	}
}
var mi = /[\t\n\r]/g;
function hi(e) {
	return e.replace(mi, "");
}
function gi(e, t) {
	return t.lastIndex = 0, t.test(e.hostname);
}
function _i(e, t) {
	return t.lastIndex = 0, t.test(e.protocol.endsWith(":") ? e.protocol.slice(0, -1) : e.protocol);
}
var vi = /*@__PURE__*/ B("$ZodURL", (e, t) => {
	K.init(e, t), e._zod.check = (n) => {
		try {
			let r = n.value.trim(), i = fi(r, t);
			if (i === 1) {
				n.issues.push({
					code: "invalid_format",
					format: "url",
					note: "Invalid URL format",
					input: n.value,
					inst: e,
					continue: !t.abort
				});
				return;
			}
			if (i === 2) {
				n.issues.push({
					code: "invalid_format",
					format: "url",
					input: n.value,
					inst: e,
					continue: !t.abort
				});
				return;
			}
			if (i === !0) {
				n.value = hi(r);
				return;
			}
			t.hostname && !gi(i, t.hostname) && n.issues.push({
				code: "invalid_format",
				format: "url",
				note: "Invalid hostname",
				pattern: t.hostname.source,
				input: n.value,
				inst: e,
				continue: !t.abort
			}), t.protocol && !_i(i, t.protocol) && n.issues.push({
				code: "invalid_format",
				format: "url",
				note: "Invalid protocol",
				pattern: t.protocol.source,
				input: n.value,
				inst: e,
				continue: !t.abort
			}), n.value = t.normalize ? i.href : hi(r);
			return;
		} catch {
			n.issues.push({
				code: "invalid_format",
				format: "url",
				input: n.value,
				inst: e,
				continue: !t.abort
			});
		}
	};
}), yi = /*@__PURE__*/ B("$ZodEmoji", (e, t) => {
	t.pattern ??= Cr(), K.init(e, t);
}), bi = /*@__PURE__*/ B("$ZodNanoID", (e, t) => {
	if (t.length !== void 0 && (!Number.isInteger(t.length) || t.length < 1)) throw Error(`Invalid nanoid length: ${t.length}`);
	t.pattern ??= t.length === void 0 ? gr : _r(t.length), K.init(e, t);
}), xi = /*@__PURE__*/ B("$ZodCUID", (e, t) => {
	t.pattern ??= dr, K.init(e, t);
}), Si = /*@__PURE__*/ B("$ZodCUID2", (e, t) => {
	t.pattern ??= fr, K.init(e, t);
}), Ci = /*@__PURE__*/ B("$ZodULID", (e, t) => {
	t.pattern ??= pr, K.init(e, t);
}), wi = /*@__PURE__*/ B("$ZodXID", (e, t) => {
	t.pattern ??= mr, K.init(e, t);
}), Ti = /*@__PURE__*/ B("$ZodKSUID", (e, t) => {
	t.pattern ??= hr, K.init(e, t);
}), Ei = /*@__PURE__*/ B("$ZodISODateTime", (e, t) => {
	t.pattern ??= Lr(t), K.init(e, t);
}), Di = /*@__PURE__*/ B("$ZodISODate", (e, t) => {
	t.pattern ??= Pr, K.init(e, t);
}), Oi = /*@__PURE__*/ B("$ZodISOTime", (e, t) => {
	t.pattern ??= Ir(t), K.init(e, t);
}), ki = /*@__PURE__*/ B("$ZodISODuration", (e, t) => {
	t.pattern ??= vr, K.init(e, t);
}), Ai = /*@__PURE__*/ B("$ZodIPv4", (e, t) => {
	t.pattern ??= wr, K.init(e, t);
}), ji = /^[0-9a-fA-F:.]+$/;
function Mi(e) {
	return ji.test(e) ? di(`http://[${e}]`) : !1;
}
var Ni = /*@__PURE__*/ B("$ZodIPv6", (e, t) => {
	t.pattern ??= Tr, K.init(e, t), e._zod.check = (n) => {
		Mi(n.value) || n.issues.push({
			code: "invalid_format",
			format: "ipv6",
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), Pi = /*@__PURE__*/ B("$ZodCIDRv4", (e, t) => {
	t.pattern ??= Er, K.init(e, t);
});
function Fi(e) {
	let t = e.split("/");
	if (t.length !== 2) return !1;
	let [n, r] = t;
	if (!r) return !1;
	let i = Number(r);
	return `${i}` !== r || i < 0 || i > 128 ? !1 : Mi(n);
}
var Ii = /*@__PURE__*/ B("$ZodCIDRv6", (e, t) => {
	t.pattern ??= Dr, K.init(e, t), e._zod.check = (n) => {
		Fi(n.value) || n.issues.push({
			code: "invalid_format",
			format: "cidrv6",
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
});
function Li(e) {
	if (e === "") return !0;
	if (/\s/.test(e) || e.length % 4 != 0) return !1;
	try {
		return atob(e), !0;
	} catch {
		return !1;
	}
}
var Ri = /^[0-9a-zA-Z+/]*={0,2}$/, zi = /*@__PURE__*/ B("$ZodBase64", (e, t) => {
	t.pattern ??= Ri, K.init(e, t), e._zod.check = (n) => {
		Li(n.value) || n.issues.push({
			code: "invalid_format",
			format: "base64",
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), Bi = /^[A-Za-z0-9_-]*$/;
function Vi(e) {
	if (!Bi.test(e)) return !1;
	let t = e.replace(/[-_]/g, (e) => e === "-" ? "+" : "/");
	return Li(t.padEnd(Math.ceil(t.length / 4) * 4, "="));
}
var Hi = /*@__PURE__*/ B("$ZodBase64URL", (e, t) => {
	t.pattern ??= Bi, K.init(e, t), e._zod.check = (n) => {
		Vi(n.value) || n.issues.push({
			code: "invalid_format",
			format: "base64url",
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), Ui = /*@__PURE__*/ B("$ZodE164", (e, t) => {
	t.pattern ??= jr, K.init(e, t);
});
function Wi(e, t = null) {
	try {
		let n = e.split(".");
		if (n.length !== 3) return !1;
		let [r] = n;
		if (!r) return !1;
		let i = JSON.parse(atob(r));
		return !("typ" in i && i?.typ !== "JWT" || !i.alg || t && (!("alg" in i) || i.alg !== t));
	} catch {
		return !1;
	}
}
var Gi = /*@__PURE__*/ B("$ZodJWT", (e, t) => {
	K.init(e, t), e._zod.check = (n) => {
		Wi(n.value, t.alg) || n.issues.push({
			code: "invalid_format",
			format: "jwt",
			input: n.value,
			inst: e,
			continue: !t.abort
		});
	};
}), Ki = /*@__PURE__*/ B("$ZodBoolean", (e, t) => {
	G.init(e, t), e._zod.pattern = Vr, e._zod.parse = (n, r) => {
		if (t.coerce) try {
			n.value = !!n.value;
		} catch {}
		let i = n.value;
		return typeof i == "boolean" || n.issues.push({
			expected: "boolean",
			code: "invalid_type",
			input: i,
			inst: e
		}), n;
	};
}), qi = /*@__PURE__*/ B("$ZodUnknown", (e, t) => {
	G.init(e, t), e._zod.parse = (e) => e;
}), Ji = /*@__PURE__*/ B("$ZodNever", (e, t) => {
	G.init(e, t), e._zod.parse = (t, n) => (t.issues.push({
		expected: "never",
		code: "invalid_type",
		input: t.value,
		inst: e
	}), t);
});
function Yi(e, t, n) {
	e.issues.length && t.issues.push(...fn(n, e.issues)), t.value[n] = e.value;
}
var Xi = /*@__PURE__*/ B("$ZodArray", (e, t) => {
	G.init(e, t);
	let n = H.memoizer;
	n?.attach(e), e._zod.parse = (r, i) => {
		let a = r.value;
		if (!Array.isArray(a)) return r.issues.push({
			expected: "array",
			code: "invalid_type",
			input: a,
			inst: e
		}), r;
		r.value = n ? n.alloc(e, r, Array(a.length), i) : Array(a.length);
		let o = [], s = i?.abortEarly;
		for (let e = 0; e < a.length; e++) {
			let n = a[e], c = t.element._zod.run({
				value: n,
				issues: []
			}, i);
			if (c instanceof Promise) o.push(c.then((t) => Yi(t, r, e)));
			else if (Yi(c, r, e), s && c.issues.length !== 0 && I(c)) break;
		}
		return o.length ? Promise.all(o).then(() => r) : r;
	};
});
function Zi(e, t, n, r, i, a) {
	let o = n in r, s = a === "optional";
	if (o || !s || i !== "optional") {
		if (e.issues.length) {
			if (i !== void 0 && s && !o) return;
			t.issues.push(...fn(n, e.issues));
		}
		!o && i === void 0 ? e.issues.length || t.issues.push({
			code: "invalid_type",
			expected: "nonoptional",
			input: void 0,
			path: [n]
		}) : e.value === void 0 ? (o || i === "defaulted" && !s) && (t.value[n] = void 0) : t.value[n] = e.value;
	}
}
var Qi = [];
function $i(e) {
	let t = Object.keys(e.shape), n = Object.getOwnPropertySymbols(e.shape), r = n.length ? n : Qi, i = r.length ? [...t, ...r] : t;
	for (let t of i) if (!e.shape?.[t]?._zod?.traits?.has("$ZodType")) throw Error(`Invalid element at key "${String(t)}": expected a Zod schema`);
	let a = Qt(e.shape);
	return {
		...e,
		allKeys: i,
		symbolKeys: r,
		keySet: new Set(t),
		numKeys: t.length,
		optionalKeys: new Set(a)
	};
}
function ea(e, t, n, r, i, a, o) {
	let s = [], c = i.keySet, l = i.catchall._zod, u = l.def.type, d = l.optin, f = l.optout, p = 0;
	for (let i in t) {
		if (o && n.issues.length !== p) {
			if (I(n, p)) break;
			p = n.issues.length;
		}
		if (c.has(i)) continue;
		if (i === "__proto__") {
			u === "never" && s.push(i);
			continue;
		}
		if (u === "never") {
			s.push(i);
			continue;
		}
		let a = l.run({
			value: t[i],
			issues: []
		}, r);
		a instanceof Promise ? e.push(a.then((e) => Zi(e, n, i, t, d, f))) : Zi(a, n, i, t, d, f);
	}
	return s.length && n.issues.push({
		code: "unrecognized_keys",
		keys: s,
		input: t,
		inst: a,
		continue: !0
	}), e.length ? Promise.all(e).then(() => n) : n;
}
var ta = /*@__PURE__*/ B("$ZodObject", (e, t) => {
	G.init(e, t);
	let n = Object.getOwnPropertyDescriptor(t, "shape"), r = n?.get ? n.get.raw : t.shape ?? {};
	if (r) {
		let e = () => {
			let n = { ...r };
			return Object.defineProperty(t, "shape", { value: n }), e.raw = n, n;
		};
		e.raw = r, Object.defineProperty(t, "shape", { get: e });
	}
	let i = Pt(() => $i(t));
	z(e, "propValues", (e) => {
		let t = e.def.shape, n = {};
		for (let e in t) {
			let r = t[e]._zod;
			if (r.values) {
				Object.prototype.hasOwnProperty.call(n, e) || j(n, e, /* @__PURE__ */ new Set());
				for (let t of r.values) n[e].add(t);
				r.optin !== void 0 && n[e].add(void 0);
			}
		}
		return n;
	});
	let a = Gt, o = t.catchall, s, c = H.memoizer;
	c?.attach(e), e._zod.parse = (t, n) => {
		s ??= i.value;
		let r = t.value;
		if (!a(r)) return t.issues.push({
			expected: "object",
			code: "invalid_type",
			input: r,
			inst: e
		}), t;
		t.value = c ? c.alloc(e, t, {}, n) : {};
		let l = [], u = s.shape, d = n?.abortEarly, f = t.issues.length;
		for (let e of s.allKeys) {
			if (d && t.issues.length !== f) {
				if (I(t, f)) break;
				f = t.issues.length;
			}
			if (e === "__proto__") continue;
			let i = u[e], a = i._zod.optin, o = i._zod.optout, s = i._zod.run({
				value: r[e],
				issues: []
			}, n);
			s instanceof Promise ? l.push(s.then((n) => Zi(n, t, e, r, a, o))) : Zi(s, t, e, r, a, o);
		}
		return o ? ea(l, r, t, n, i.value, e, d === !0) : l.length ? Promise.all(l).then(() => t) : t;
	};
}), na = /*@__PURE__*/ B("$ZodObjectJIT", (e, t) => {
	ta.init(e, t);
	let n = e._zod.parse, r = Pt(() => $i(t)), i = H.memoizer, a = (t) => {
		let n = r.value, a = n.symbolKeys, o = new ni(["payload", "ctx"], {
			shape: t,
			inst: e,
			memo: i,
			syms: a
		}), s = (e) => `shape[${e}]._zod.run({ value: input[${e}], issues: [] }, ctx)`, c = (e, t) => `
          let ${e}_ab = false;
          for (let i = 0; i < ${e}.issues.length; i++) {
            const iss = ${e}.issues[i];
            iss.path = iss.path ? [${t}, ...iss.path] : [${t}];
            payload.issues.push(iss);
            if (iss.continue !== true) ${e}_ab = true;
          }
          if (${e}_ab && ctx && ctx.abortEarly) {
            payload.value = newResult;
            return payload;
          }`;
		o.write("const input = payload.value;");
		let l = Object.create(null), u = 0;
		for (let e of n.allKeys) l[e] = `key_${u++}`;
		o.write(i ? "const newResult = memo.alloc(inst, payload, {}, ctx);" : "const newResult = {};");
		for (let e of n.allKeys) {
			if (e === "__proto__") continue;
			let n = l[e], r = typeof e == "symbol" ? `syms[${a.indexOf(e)}]` : Ht(e), i = `${r} in input`, u = t[e], d = u?._zod?.optin, f = d !== void 0, p = u?._zod?.optout === "optional";
			if (o.write(`const ${n} = ${s(r)};`), f && p) {
				let e = d === "optional" ? `${n}_present` : `${n}.value !== undefined || ${n}_present`;
				o.write(`
        const ${n}_present = ${i};
        if (!${n}.issues.length || ${n}_present) {
          if (${n}.issues.length) {${c(n, r)}
          }

          if (${e}) {
            newResult[${r}] = ${n}.value;
          }
        }

      `);
			} else f ? (o.write(`
        if (${n}.issues.length) {${c(n, r)}
        }
      `), d === "defaulted" ? o.write(`newResult[${r}] = ${n}.value;`) : o.write(`
        if (${n}.value !== undefined || ${i}) {
          newResult[${r}] = ${n}.value;
        }
      `)) : o.write(`
        const ${n}_present = ${i};
        if (${n}.issues.length) {${c(n, r)}
        }
        if (!${n}_present && !${n}.issues.length) {
          payload.issues.push({
            code: "invalid_type",
            expected: "nonoptional",
            input: undefined,
            path: [${r}]
          });
          if (ctx && ctx.abortEarly) {
            payload.value = newResult;
            return payload;
          }
        }

        if (${n}_present) {
          newResult[${r}] = ${n}.value;
        }

      `);
		}
		return o.write("payload.value = newResult;"), o.write("return payload;"), o.compile();
	}, o, s = Gt, c = !H.jitless, l = c && Kt.value, u = t.catchall, d;
	e._zod.parse = (i, f) => {
		d ??= r.value;
		let p = i.value;
		return s(p) ? c && l && f?.async === !1 && f.jitless !== !0 ? (o ||= a(t.shape), i = o(i, f), u ? ea([], p, i, f, d, e, f?.abortEarly === !0) : i) : n(i, f) : (i.issues.push({
			expected: "object",
			code: "invalid_type",
			input: p,
			inst: e
		}), i);
	};
});
function ra(e, t, n, r) {
	for (let n of e) if (n.issues.length === 0) return t.value = n.value, t;
	let i = e.filter((e) => !I(e));
	return i.length === 1 ? (t.value = i[0].value, i[0]) : (t.issues.push({
		code: "invalid_union",
		input: t.value,
		inst: n,
		errors: e.map((e) => e.issues.map((e) => L(e, r, U())))
	}), t);
}
var ia = /*@__PURE__*/ B("$ZodUnion", (e, t) => {
	G.init(e, t), z(e, "optin", (e) => e.def.options.some((e) => e._zod.optin === "defaulted") ? "defaulted" : e.def.options.some((e) => e._zod.optin !== void 0) ? "optional" : void 0), z(e, "optout", (e) => e.def.options.some((e) => e._zod.optout === "optional") ? "optional" : void 0), z(e, "values", (e) => {
		if (e.def.options.every((e) => e._zod.values)) return new Set(e.def.options.flatMap((e) => Array.from(e._zod.values)));
	}), z(e, "pattern", (e) => {
		if (e.def.options.every((e) => e._zod.pattern)) {
			let t = e.def.options.map((e) => e._zod.pattern);
			return RegExp(`^(${t.map((e) => It(e.source)).join("|")})$`);
		}
	});
	let n = t.options.length === 1 ? t.options[0]._zod.run : null;
	e._zod.parse = (r, i) => {
		if (n) return n(r, i);
		let a = !1, o = [];
		for (let e of t.options) {
			let t = e._zod.run({
				value: r.value,
				issues: []
			}, i);
			if (t instanceof Promise) o.push(t), a = !0;
			else {
				if (t.issues.length === 0) return t;
				o.push(t);
			}
		}
		return a ? Promise.all(o).then((t) => ra(t, r, e, i)) : ra(o, r, e, i);
	};
}), aa = /*@__PURE__*/ B("$ZodIntersection", (e, t) => {
	G.init(e, t), e._zod.parse = (e, n) => {
		let r = e.value, i = t.left._zod.run({
			value: r,
			issues: []
		}, n), a = t.right._zod.run({
			value: r,
			issues: []
		}, n);
		return i instanceof Promise || a instanceof Promise ? Promise.all([i, a]).then(([t, n]) => sa(e, t, n)) : sa(e, i, a);
	};
});
function oa(e, t) {
	if (e === t || e instanceof Date && t instanceof Date && +e == +t) return {
		valid: !0,
		data: e
	};
	if (qt(e) && qt(t)) {
		let n = Object.keys(t), r = Object.keys(e).filter((e) => n.indexOf(e) !== -1), i = {
			...e,
			...t
		};
		Object.prototype.hasOwnProperty.call(i, "__proto__") && delete i.__proto__;
		for (let n of r) {
			if (n === "__proto__") continue;
			let r = oa(e[n], t[n]);
			if (!r.valid) return {
				valid: !1,
				mergeErrorPath: [n, ...r.mergeErrorPath]
			};
			i[n] = r.data;
		}
		return {
			valid: !0,
			data: i
		};
	}
	if (Array.isArray(e) && Array.isArray(t)) {
		if (e.length !== t.length) return {
			valid: !1,
			mergeErrorPath: []
		};
		let n = [];
		for (let r = 0; r < e.length; r++) {
			let i = e[r], a = t[r], o = oa(i, a);
			if (!o.valid) return {
				valid: !1,
				mergeErrorPath: [r, ...o.mergeErrorPath]
			};
			n.push(o.data);
		}
		return {
			valid: !0,
			data: n
		};
	}
	return {
		valid: !1,
		mergeErrorPath: []
	};
}
function sa(e, t, n) {
	let r = /* @__PURE__ */ new Map(), i, a = /* @__PURE__ */ new Map(), o = (e, t) => {
		let n;
		if (e.code === "unrecognized_keys" && !e.path?.length) i ??= e, n = e.keys;
		else if (e.code === "invalid_key" && e.origin === "record" && e.path?.length === 1) {
			let t = String(e.path[0]);
			a.has(t) || a.set(t, e), n = [t];
		} else return !1;
		for (let e of n) r.has(e) || r.set(e, {}), r.get(e)[t] = !0;
		return !0;
	};
	for (let n of t.issues) o(n, "l") || e.issues.push(n);
	for (let t of n.issues) o(t, "r") || e.issues.push(t);
	let s = [...r].filter(([, e]) => e.l && e.r).map(([e]) => e);
	if (s.length) {
		let t = i ? s.filter((e) => i.keys.includes(e)) : [];
		t.length && e.issues.push({
			...i,
			keys: t
		});
		for (let n of s) !t.includes(n) && a.has(n) && e.issues.push(a.get(n));
	}
	let c = oa(t.value, n.value);
	if (!c.valid) {
		if (I(e)) return e;
		throw Error(`Unmergable intersection. Error path: ${JSON.stringify(c.mergeErrorPath)}`);
	}
	return e.value = c.data, e;
}
var ca = /*@__PURE__*/ B("$ZodRecord", (e, t) => {
	G.init(e, t);
	let n = H.memoizer;
	n?.attach(e), e._zod.parse = (r, i) => {
		let a = r.value;
		if (!qt(a)) return r.issues.push({
			expected: "record",
			code: "invalid_type",
			input: a,
			inst: e
		}), r;
		let o = [], s = t.keyType._zod.values;
		if (s && !t.partial) {
			r.value = n ? n.alloc(e, r, {}, i) : {};
			let c = /* @__PURE__ */ new Set();
			for (let n of s) if (typeof n == "string" || typeof n == "number" || typeof n == "symbol") {
				if (c.add(typeof n == "number" ? n.toString() : n), n === "__proto__") continue;
				let s = t.keyType._zod.run({
					value: n,
					issues: []
				}, i);
				if (s instanceof Promise) throw Error("Async schemas not supported in object keys currently");
				if (s.issues.length) {
					r.issues.push({
						code: "invalid_key",
						origin: "record",
						issues: s.issues.map((e) => L(e, i, U())),
						input: n,
						path: [n],
						inst: e
					});
					continue;
				}
				let l = s.value;
				if (l === "__proto__") continue;
				let u = t.valueType._zod.run({
					value: a[n],
					issues: []
				}, i);
				u instanceof Promise ? o.push(u.then((e) => {
					e.issues.length && r.issues.push(...fn(n, e.issues)), r.value[l] = e.value;
				})) : (u.issues.length && r.issues.push(...fn(n, u.issues)), r.value[l] = u.value);
			}
			let l;
			for (let e in a) if (!c.has(e)) {
				if (t.mode === "loose") {
					if (e === "__proto__") continue;
					r.value[e] = a[e];
				} else l ??= [], l.push(e);
			}
			l && l.length > 0 && r.issues.push({
				code: "unrecognized_keys",
				input: a,
				inst: e,
				keys: l,
				continue: !0
			});
		} else {
			r.value = n ? n.alloc(e, r, {}, i) : {};
			let c;
			for (let n of Reflect.ownKeys(a)) {
				if (n === "__proto__" || !Object.prototype.propertyIsEnumerable.call(a, n)) continue;
				let l = t.keyType._zod.run({
					value: n,
					issues: []
				}, i);
				if (l instanceof Promise) throw Error("Async schemas not supported in object keys currently");
				if (typeof n == "string" && Br.test(n) && l.issues.length) {
					let e = t.keyType._zod.run({
						value: Number(n),
						issues: []
					}, i);
					if (e instanceof Promise) throw Error("Async schemas not supported in object keys currently");
					e.issues.length === 0 && (l = e);
				}
				if (l.issues.length) {
					t.mode === "loose" ? r.value[n] = a[n] : s ? (c ??= [], c.push(n)) : r.issues.push({
						code: "invalid_key",
						origin: "record",
						issues: l.issues.map((e) => L(e, i, U())),
						input: n,
						path: [n],
						inst: e
					});
					continue;
				}
				let u = l.value;
				if (u === "__proto__") continue;
				let d = t.valueType._zod.run({
					value: a[n],
					issues: []
				}, i);
				d instanceof Promise ? o.push(d.then((e) => {
					e.issues.length && r.issues.push(...fn(n, e.issues)), r.value[u] = e.value;
				})) : (d.issues.length && r.issues.push(...fn(n, d.issues)), r.value[u] = d.value);
			}
			c && c.length > 0 && r.issues.push({
				code: "unrecognized_keys",
				input: a,
				inst: e,
				keys: c,
				continue: !0
			});
		}
		return o.length ? Promise.all(o).then(() => r) : r;
	};
}), la = /*@__PURE__*/ B("$ZodEnum", (e, t) => {
	G.init(e, t);
	let n = At(t.entries), r = new Set(n);
	e._zod.values = r, z(e, "pattern", (e) => {
		let t = At(e.def.entries).filter((e) => Yt.has(typeof e));
		return RegExp(t.length ? `^(${t.map((e) => Xt(e.toString())).join("|")})$` : "^[^\\s\\S]$");
	}), e._zod.parse = (t, i) => {
		let a = t.value;
		return r.has(a) || t.issues.push({
			code: "invalid_value",
			values: n,
			input: a,
			inst: e
		}), t;
	};
}), ua = /*@__PURE__*/ B("$ZodLiteral", (e, t) => {
	G.init(e, t);
	let n = new Set(t.values);
	e._zod.values = n, z(e, "pattern", (e) => {
		let t = e.def.values;
		return RegExp(t.length ? `^(${t.map((e) => typeof e == "string" ? Xt(e) : e ? Xt(e.toString()) : String(e)).join("|")})$` : "^[^\\s\\S]$");
	}), e._zod.parse = (r, i) => {
		let a = r.value;
		return n.has(a) || r.issues.push({
			code: "invalid_value",
			values: t.values,
			input: a,
			inst: e
		}), r;
	};
}), da = /*@__PURE__*/ B("$ZodTransform", (e, t) => {
	G.init(e, t), e._zod.optin = "optional", H.memoizer?.guard(e), e._zod.parse = (n, r) => {
		if (r.direction === "backward") throw new Fn(e.constructor.name);
		let i = t.transform(n.value, n);
		if (r.async) return (i instanceof Promise ? i : Promise.resolve(i)).then((e) => (n.value = e, n));
		if (i instanceof Promise) throw new V();
		return n.value = i, n;
	};
});
function fa(e, t) {
	return e.value = t.issues.length ? void 0 : t.value, e;
}
var pa = /*@__PURE__*/ B("$ZodOptional", (e, t) => {
	G.init(e, t), z(e, "optin", (e) => e.def.innerType._zod.optin === "defaulted" ? "defaulted" : "optional"), e._zod.optout = "optional", z(e, "values", (e) => {
		let t = e.def.innerType._zod.values;
		return t ? /* @__PURE__ */ new Set([...t, void 0]) : void 0;
	}), z(e, "pattern", (e) => {
		let t = e.def.innerType._zod.pattern;
		return t ? RegExp(`^(${It(t.source)})?$`) : void 0;
	}), e._zod.parse = (e, n) => {
		if (e.value === void 0) {
			if (t.innerType._zod.optin !== "defaulted") return e;
			let r = t.innerType._zod.run({
				value: e.value,
				issues: []
			}, n);
			return r instanceof Promise ? r.then((t) => fa(e, t)) : fa(e, r);
		}
		return t.innerType._zod.run(e, n);
	};
}), ma = /*@__PURE__*/ B("$ZodExactOptional", (e, t) => {
	pa.init(e, t), z(e, "values", (e) => e.def.innerType._zod.values), z(e, "pattern", (e) => e.def.innerType._zod.pattern), e._zod.parse = (e, n) => t.innerType._zod.run(e, n);
}), ha = /*@__PURE__*/ B("$ZodNullable", (e, t) => {
	G.init(e, t), z(e, "optin", (e) => e.def.innerType._zod.optin), z(e, "optout", (e) => e.def.innerType._zod.optout), z(e, "pattern", (e) => {
		let t = e.def.innerType._zod.pattern;
		return t ? RegExp(`^(${It(t.source)}|null)$`) : void 0;
	}), z(e, "values", (e) => e.def.innerType._zod.values ? /* @__PURE__ */ new Set([...e.def.innerType._zod.values, null]) : void 0), e._zod.parse = (e, n) => e.value === null ? e : t.innerType._zod.run(e, n);
}), ga = /*@__PURE__*/ B("$ZodDefault", (e, t) => {
	G.init(e, t), e._zod.optin = "defaulted", z(e, "values", (e) => e.def.innerType._zod.values), e._zod.parse = (e, n) => {
		if (n.direction === "backward") return t.innerType._zod.run(e, n);
		if (e.value === void 0) return e.value = t.defaultValue, e;
		let r = t.innerType._zod.run(e, n);
		return r instanceof Promise ? r.then((e) => _a(e, t)) : _a(r, t);
	};
});
function _a(e, t) {
	return e.value === void 0 && (e.value = t.defaultValue), e;
}
var va = /*@__PURE__*/ B("$ZodPrefault", (e, t) => {
	G.init(e, t), e._zod.optin = "defaulted", z(e, "values", (e) => e.def.innerType._zod.values), e._zod.parse = (e, n) => (n.direction === "backward" || e.value === void 0 && (e.value = t.defaultValue), t.innerType._zod.run(e, n));
}), ya = /*@__PURE__*/ B("$ZodNonOptional", (e, t) => {
	G.init(e, t), z(e, "values", (e) => {
		let t = e.def.innerType._zod.values;
		return t ? new Set([...t].filter((e) => e !== void 0)) : void 0;
	}), e._zod.parse = (n, r) => {
		let i = t.innerType._zod.run(n, r);
		return i instanceof Promise ? i.then((t) => ba(t, e)) : ba(i, e);
	};
});
function ba(e, t) {
	return !e.issues.length && e.value === void 0 && e.issues.push({
		code: "invalid_type",
		expected: "nonoptional",
		input: e.value,
		inst: t
	}), e;
}
function xa(e, t, n, r) {
	return t.issues.length ? (e.value = n.catchValue({
		...t,
		value: e.value,
		error: { issues: t.issues.map((e) => L(e, r, U())) },
		input: e.value
	}), e) : (e.value = t.value, t.memo && (e.memo = !0), e);
}
var Sa = /*@__PURE__*/ B("$ZodCatch", (e, t) => {
	G.init(e, t), z(e, "optin", (e) => e.def.innerType._zod.optin === "defaulted" ? "defaulted" : "optional"), z(e, "optout", (e) => e.def.innerType._zod.optout), z(e, "values", (e) => e.def.innerType._zod.values), e._zod.parse = (e, n) => {
		if (n.direction === "backward") return t.innerType._zod.run(e, n);
		let r = t.innerType._zod.run({
			value: e.value,
			issues: []
		}, n);
		return r instanceof Promise ? r.then((r) => xa(e, r, t, n)) : xa(e, r, t, n);
	};
}), Ca = /*@__PURE__*/ B("$ZodPipe", (e, t) => {
	G.init(e, t), z(e, "values", (e) => e.def.in._zod.values), z(e, "optin", (e) => e.def.in._zod.optin), z(e, "optout", (e) => e.def.out._zod.optout), z(e, "propValues", (e) => e.def.in._zod.propValues), e._zod.parse = (e, n) => {
		if (n.direction === "backward") {
			let r = t.out._zod.run(e, n);
			return r instanceof Promise ? r.then((e) => wa(e, t.in, n)) : wa(r, t.in, n);
		}
		let r = t.in._zod.run(e, n);
		return r instanceof Promise ? r.then((e) => wa(e, t.out, n)) : wa(r, t.out, n);
	};
});
function wa(e, t, n) {
	return e.issues.some((e) => e.code !== "unrecognized_keys") ? (e.aborted = !0, e) : t._zod.run({
		value: e.value,
		issues: e.issues
	}, n);
}
var Ta = /*@__PURE__*/ B("$ZodReadonly", (e, t) => {
	G.init(e, t), z(e, "propValues", (e) => e.def.innerType._zod.propValues), z(e, "values", (e) => e.def.innerType._zod.values), z(e, "optin", (e) => e.def.innerType?._zod?.optin), z(e, "optout", (e) => e.def.innerType?._zod?.optout), e._zod.parse = (e, n) => {
		if (n.direction === "backward") return t.innerType._zod.run(e, n);
		let r = t.innerType._zod.run(e, n);
		return r instanceof Promise ? r.then(Ea) : Ea(r);
	};
});
function Ea(e) {
	return e.memo || (e.value = Object.freeze(e.value)), e;
}
var Da = /*@__PURE__*/ B("$ZodCustom", (e, t) => {
	W.init(e, t), G.init(e, t), e._zod.parse = (e, t) => e, e._zod.check = (n) => {
		let r = n.value, i = t.fn(r);
		if (i instanceof Promise) return i.then((t) => Oa(t, n, r, e));
		Oa(i, n, r, e);
	};
});
function Oa(e, t, n, r) {
	if (!e) {
		let e = {
			code: "custom",
			input: n,
			inst: r,
			path: [...r._zod.def.path ?? []],
			continue: !r._zod.def.abort
		};
		r._zod.def.params && (e.params = r._zod.def.params), t.issues.push(yn(e));
	}
}
//#endregion
//#region node_modules/.pnpm/zod@4.6.5/node_modules/zod/v4/core/memoizer.js
var ka = class extends Error {
	constructor() {
		super("Cannot parse a reference cycle that closes through a transform"), this.name = "ZodCyclicError";
	}
}, Aa = "~memo", ja = [];
function Ma(e) {
	return typeof e == "object" && !!e;
}
function Na(e) {
	return e.map((e) => e.path ? {
		...e,
		path: e.path.slice()
	} : { ...e });
}
var Pa = /*@__PURE__*/ new WeakMap(), Fa = 0, Ia = 1, La = 2;
function Ra(e, t, n) {
	let r = Pa.get(e);
	if (r !== void 0) return r ? La : Fa;
	if (t.has(e)) return La;
	t.add(e);
	let i = Fa, a = (e) => {
		if (i !== La && e?._zod) {
			let r = Ra(e, t, n);
			r > i && (i = r);
		}
	}, o = (e, r) => {
		let i = Fa;
		for (let a of Reflect.ownKeys(e)) {
			let o = Object.getOwnPropertyDescriptor(e, a);
			if (r && !o.enumerable) continue;
			let s = o.get ? Ia : o.value?._zod ? Ra(o.value, t, n) : Fa;
			s > i && (i = s);
		}
		return i;
	}, s = (e) => {
		e > i && (i = e);
	}, c = e._zod.def;
	switch (c.type) {
		case "object": {
			let e = Lt(c);
			s(e ? o(e, !0) : Ia), a(c.catchall);
			break;
		}
		case "array":
			a(c.element);
			break;
		case "tuple":
			for (let e of c.items) a(e);
			a(c.rest);
			break;
		case "record":
		case "map":
			a(c.keyType), a(c.valueType);
			break;
		case "set":
			a(c.valueType);
			break;
		case "union":
			for (let e of c.options) a(e);
			break;
		case "intersection":
			a(c.left), a(c.right);
			break;
		case "optional":
		case "nullable":
		case "default":
		case "prefault":
		case "catch":
		case "readonly":
		case "nonoptional":
		case "promise":
		case "success":
			a(c.innerType);
			break;
		case "pipe":
			a(c.in), a(c.out);
			break;
		case "function":
			a(c.input), a(c.output);
			break;
		case "lazy": {
			let r = c._cachedInner ?? (n ? e._zod.innerType : void 0);
			s(r ? Ra(r, t, !1) : Ia);
			break;
		}
		case "template_literal":
		case "string":
		case "number":
		case "int":
		case "boolean":
		case "bigint":
		case "symbol":
		case "undefined":
		case "null":
		case "void":
		case "never":
		case "any":
		case "unknown":
		case "date":
		case "nan":
		case "enum":
		case "literal":
		case "file":
		case "transform":
		case "custom": break;
		default: for (let e in c) {
			let t = Object.getOwnPropertyDescriptor(c, e);
			if (!t || t.get) continue;
			let n = t.value;
			if (n && typeof n == "object") {
				if (n._zod) a(n);
				else if (Array.isArray(n)) for (let e of n) a(e);
			}
		}
	}
	return t.delete(e), za(e, i);
}
function za(e, t) {
	return t !== Ia && Pa.set(e, t === La), t;
}
function Ba(e, t) {
	let n = e.buckets.get(t);
	return n || (n = /* @__PURE__ */ new WeakMap(), e.buckets.set(t, n)), n;
}
var Va, Ha = [], Ua = {
	alloc(e, t, n) {
		let r = Va;
		if (!r) return n;
		Va = void 0;
		let i = {
			value: n,
			issues: null
		};
		return r.set(t.value, i), Ha.push(i), n;
	},
	guard(e) {
		var t;
		(t = e._zod).deferred ?? (t.deferred = []), e._zod.deferred.push(() => {
			let t = e._zod.parse, n = (e, n) => {
				if (n.direction !== "backward" && Ga(n, e.value)) throw new ka();
				return t(e, n);
			};
			e._zod.parse = n, e._zod.run === t && (e._zod.run = n);
		});
	},
	attach(e) {
		var t;
		let n, r = !1, i, a;
		(t = e._zod).deferred ?? (t.deferred = []), e._zod.deferred.push(() => {
			let t = e._zod.parse, o = (s, c) => {
				if (n === void 0) {
					let i = Ra(e, /* @__PURE__ */ new Set(), !1);
					if (i === Fa) return e._zod.parse = t, e._zod.run === o && (e._zod.run = t), t(s, c);
					i === La || r ? n = !0 : r = !0;
				}
				let l = s.value;
				if (!Ma(l)) return t(s, c);
				let u = c[Aa];
				u || (u = {
					buckets: /* @__PURE__ */ new WeakMap(),
					backEdges: void 0
				}, c[Aa] = u);
				let d;
				i === c ? d = a : (d = Ba(u, e), i = c, a = d);
				let f = d.get(l);
				if (f) return s.value = f.value, f.issues ? f.issues.length && s.issues.push(...Na(f.issues)) : (s.memo = !0, u.backEdges ?? (u.backEdges = /* @__PURE__ */ new WeakSet()), u.backEdges.add(f.value)), s;
				Va = d;
				let p = Ha.length, m = t(s, c);
				Va = void 0;
				let h = Ha.length > p ? Ha.pop() : void 0;
				return m instanceof Promise ? m.then((e) => (h && (h.issues = e.issues.length ? Na(e.issues) : ja), e)) : (h && (h.issues = m.issues.length ? Na(m.issues) : ja), m);
			};
			e._zod.parse = o, e._zod.run === t && (e._zod.run = o);
		});
	}
};
function Wa() {
	return Ua;
}
function Ga(e, t) {
	let n = e[Aa]?.backEdges;
	return n !== void 0 && Ma(t) && n.has(t);
}
//#endregion
//#region node_modules/.pnpm/zod@4.6.5/node_modules/zod/v4/locales/en.js
var Ka = () => {
	let e = {
		string: {
			unit: "characters",
			verb: "to have"
		},
		file: {
			unit: "bytes",
			verb: "to have"
		},
		array: {
			unit: "items",
			verb: "to have"
		},
		set: {
			unit: "items",
			verb: "to have"
		},
		map: {
			unit: "entries",
			verb: "to have"
		}
	};
	function t(t) {
		return e[t] ?? null;
	}
	let n = {
		regex: "input",
		email: "email address",
		url: "URL",
		emoji: "emoji",
		uuid: "UUID",
		uuidv4: "UUIDv4",
		uuidv6: "UUIDv6",
		nanoid: "nanoid",
		guid: "GUID",
		cuid: "cuid",
		cuid2: "cuid2",
		ulid: "ULID",
		xid: "XID",
		ksuid: "KSUID",
		datetime: "ISO datetime",
		date: "ISO date",
		time: "ISO time",
		duration: "ISO duration",
		ipv4: "IPv4 address",
		ipv6: "IPv6 address",
		mac: "MAC address",
		cidrv4: "IPv4 range",
		cidrv6: "IPv6 range",
		base64: "base64-encoded string",
		base64url: "base64url-encoded string",
		json_string: "JSON string",
		e164: "E.164 number",
		currency_code: "currency code",
		credit_card: "credit card number",
		iban: "IBAN",
		jwt: "JWT",
		template_literal: "input"
	}, r = { nan: "NaN" };
	function i(e, t) {
		return e === "number" && typeof t == "number" && !Number.isFinite(t) ? String(t) : r[e] ?? e;
	}
	return (e) => {
		switch (e.code) {
			case "invalid_type": return `Invalid input: expected ${i(e.expected)}, received ${i(vn(e.input), e.input)}`;
			case "invalid_value": return e.values.length === 1 ? `Invalid input: expected ${Zt(e.values[0])}` : `Invalid option: expected one of ${jt(e.values, "|")}`;
			case "too_big": {
				let n = e.exact ? "exactly " : e.inclusive ? "<=" : "<", r = t(e.origin);
				return r ? `Too big: expected ${e.origin ?? "value"} to have ${n}${e.maximum.toString()} ${r.unit ?? "elements"}` : `Too big: expected ${e.origin ?? "value"} to be ${n}${e.maximum.toString()}`;
			}
			case "too_small": {
				let n = e.exact ? "exactly " : e.inclusive ? ">=" : ">", r = t(e.origin);
				return r ? `Too small: expected ${e.origin} to have ${n}${e.minimum.toString()} ${r.unit}` : `Too small: expected ${e.origin} to be ${n}${e.minimum.toString()}`;
			}
			case "invalid_format": {
				let t = e;
				return t.format === "starts_with" ? `Invalid string: must start with "${t.prefix}"` : t.format === "ends_with" ? `Invalid string: must end with "${t.suffix}"` : t.format === "includes" ? `Invalid string: must include "${t.includes}"` : t.format === "regex" ? `Invalid string: must match pattern ${t.pattern}` : `Invalid ${n[t.format] ?? e.format}`;
			}
			case "not_multiple_of": return `Invalid number: must be a multiple of ${e.divisor}`;
			case "unrecognized_keys": return `Unrecognized key${e.keys.length > 1 ? "s" : ""}: ${jt(e.keys, ", ")}`;
			case "invalid_key": return `Invalid key in ${e.origin}`;
			case "invalid_union": return e.options && Array.isArray(e.options) && e.options.length > 0 ? `Invalid discriminator value. Expected ${e.options.map((e) => `'${e}'`).join(" | ")}` : e.inclusive === !1 ? "Invalid input: more than one option matched" : "Invalid input";
			case "invalid_element": return `Invalid value in ${e.origin}`;
			default: return "Invalid input";
		}
	};
};
function qa() {
	return { localeError: Ka() };
}
//#endregion
//#region node_modules/.pnpm/zod@4.6.5/node_modules/zod/v4/core/registries.js
var Ja, Ya = class {
	constructor() {
		this._map = /* @__PURE__ */ new WeakMap(), this._idmap = /* @__PURE__ */ new Map();
	}
	add(e, ...t) {
		let n = t[0];
		return this._map.set(e, n), n && typeof n == "object" && "id" in n && this._idmap.set(n.id, e), this;
	}
	clear() {
		return this._map = /* @__PURE__ */ new WeakMap(), this._idmap = /* @__PURE__ */ new Map(), this;
	}
	remove(e) {
		let t = this._map.get(e);
		return t && typeof t == "object" && "id" in t && this._idmap.delete(t.id), this._map.delete(e), this;
	}
	get(e) {
		let t = e._zod.parent;
		if (t) {
			let n = { ...this.get(t) ?? {} };
			delete n.id;
			let r = {
				...n,
				...this._map.get(e)
			};
			return Object.keys(r).length ? r : void 0;
		}
		return this._map.get(e);
	}
	has(e) {
		return this._map.has(e);
	}
};
function Xa() {
	return new Ya();
}
(Ja = globalThis).__zod_globalRegistry ?? (Ja.__zod_globalRegistry = Xa());
var Za = globalThis.__zod_globalRegistry;
//#endregion
//#region node_modules/.pnpm/zod@4.6.5/node_modules/zod/v4/core/api.js
function Qa(e) {
	return e.checks &&= [...e.checks], e;
}
// @__NO_SIDE_EFFECTS__
function $a(e, t) {
	return new e(Qa({
		type: "string",
		...F(t)
	}));
}
// @__NO_SIDE_EFFECTS__
function eo(e, t) {
	return new e({
		type: "string",
		format: "email",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function to(e, t) {
	return new e({
		type: "string",
		format: "guid",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function no(e, t) {
	return new e({
		type: "string",
		format: "uuid",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function ro(e, t) {
	return new e({
		type: "string",
		format: "uuid",
		check: "string_format",
		abort: !1,
		version: "v4",
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function io(e, t) {
	return new e({
		type: "string",
		format: "uuid",
		check: "string_format",
		abort: !1,
		version: "v6",
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function ao(e, t) {
	return new e({
		type: "string",
		format: "uuid",
		check: "string_format",
		abort: !1,
		version: "v7",
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function oo(e, t) {
	return new e({
		type: "string",
		format: "url",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function so(e, t) {
	return new e({
		type: "string",
		format: "emoji",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function co(e, t) {
	return new e({
		type: "string",
		format: "nanoid",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function lo(e, t) {
	return new e({
		type: "string",
		format: "cuid",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function uo(e, t) {
	return new e({
		type: "string",
		format: "cuid2",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function fo(e, t) {
	return new e({
		type: "string",
		format: "ulid",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function po(e, t) {
	return new e({
		type: "string",
		format: "xid",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function mo(e, t) {
	return new e({
		type: "string",
		format: "ksuid",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function ho(e, t) {
	return new e({
		type: "string",
		format: "ipv4",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function go(e, t) {
	return new e({
		type: "string",
		format: "ipv6",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function _o(e, t) {
	return new e({
		type: "string",
		format: "cidrv4",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function vo(e, t) {
	return new e({
		type: "string",
		format: "cidrv6",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function yo(e, t) {
	return new e({
		type: "string",
		format: "base64",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function bo(e, t) {
	return new e({
		type: "string",
		format: "base64url",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function xo(e, t) {
	return new e({
		type: "string",
		format: "e164",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function So(e, t) {
	return new e({
		type: "string",
		format: "jwt",
		check: "string_format",
		abort: !1,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function Co(e, t) {
	return new e({
		type: "string",
		format: "datetime",
		check: "string_format",
		offset: !1,
		local: !1,
		precision: null,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function wo(e, t) {
	return new e({
		type: "string",
		format: "date",
		check: "string_format",
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function To(e, t) {
	return new e({
		type: "string",
		format: "time",
		check: "string_format",
		precision: null,
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function Eo(e, t) {
	return new e({
		type: "string",
		format: "duration",
		check: "string_format",
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function Do(e, t) {
	return new e({
		type: "boolean",
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function Oo(e) {
	return new e({ type: "unknown" });
}
// @__NO_SIDE_EFFECTS__
function ko(e, t) {
	return new e({
		type: "never",
		...F(t)
	});
}
// @__NO_SIDE_EFFECTS__
function Ao(e, t) {
	return new Gr({
		check: "max_length",
		...F(t),
		maximum: e
	});
}
// @__NO_SIDE_EFFECTS__
function jo(e, t) {
	return new Kr({
		check: "min_length",
		...F(t),
		minimum: e
	});
}
// @__NO_SIDE_EFFECTS__
function Mo(e, t) {
	return new qr({
		check: "length_equals",
		...F(t),
		length: e
	});
}
// @__NO_SIDE_EFFECTS__
function No(e, t) {
	return new Yr({
		check: "string_format",
		format: "regex",
		...F(t),
		pattern: e
	});
}
// @__NO_SIDE_EFFECTS__
function Po(e) {
	return new Xr({
		check: "string_format",
		format: "lowercase",
		...F(e)
	});
}
// @__NO_SIDE_EFFECTS__
function Fo(e) {
	return new Zr({
		check: "string_format",
		format: "uppercase",
		...F(e)
	});
}
// @__NO_SIDE_EFFECTS__
function Io(e, t) {
	return new Qr({
		check: "string_format",
		format: "includes",
		...F(t),
		includes: e
	});
}
// @__NO_SIDE_EFFECTS__
function Lo(e, t) {
	return new $r({
		check: "string_format",
		format: "starts_with",
		...F(t),
		prefix: e
	});
}
// @__NO_SIDE_EFFECTS__
function Ro(e, t) {
	return new ei({
		check: "string_format",
		format: "ends_with",
		...F(t),
		suffix: e
	});
}
// @__NO_SIDE_EFFECTS__
function zo(e) {
	return new ti({
		check: "overwrite",
		tx: e
	});
}
// @__NO_SIDE_EFFECTS__
function Bo(e) {
	return /* @__PURE__ */ zo((t) => t.normalize(e));
}
// @__NO_SIDE_EFFECTS__
function Vo() {
	return /* @__PURE__ */ zo((e) => e.trim());
}
// @__NO_SIDE_EFFECTS__
function Ho() {
	return /* @__PURE__ */ zo((e) => e.toLowerCase());
}
// @__NO_SIDE_EFFECTS__
function Uo() {
	return /* @__PURE__ */ zo((e) => e.toUpperCase());
}
// @__NO_SIDE_EFFECTS__
function Wo() {
	return /* @__PURE__ */ zo((e) => Ut(e));
}
// @__NO_SIDE_EFFECTS__
function Go(e, t, n) {
	return new e({
		type: "array",
		element: t,
		...F(n)
	});
}
// @__NO_SIDE_EFFECTS__
function Ko(e, t, n) {
	return new e({
		type: "custom",
		check: "custom",
		fn: t,
		...F(n)
	});
}
// @__NO_SIDE_EFFECTS__
function qo(e, t) {
	let n = /* @__PURE__ */ Jo((t) => (t.addIssue = (e) => {
		if (typeof e == "string") t.issues.push(yn(e, t.value, n._zod.def));
		else {
			let r = e;
			r.fatal && (r.continue = !1), r.code ??= "custom", "input" in r || (r.input = t.value), r.inst ??= n, r.continue ??= !n._zod.def.abort, t.issues.push(yn(r));
		}
	}, e(t.value, t)), t);
	return n;
}
// @__NO_SIDE_EFFECTS__
function Jo(e, t) {
	let n = new W({
		check: "custom",
		...F(t)
	});
	return n._zod.check = e, n;
}
//#endregion
//#region node_modules/.pnpm/zod@4.6.5/node_modules/zod/v4/core/to-json-schema.js
function Yo(e, ...t) {
	for (let n of t) for (let t of Reflect.ownKeys(n)) Object.prototype.propertyIsEnumerable.call(n, t) && j(e, t, n[t]);
	return e;
}
function Xo(e) {
	let t = e?.target ?? "draft-2020-12";
	return t === "draft-4" && (t = "draft-04"), t === "draft-7" && (t = "draft-07"), {
		processors: e.processors ?? {},
		metadataRegistry: e?.metadata ?? Za,
		target: t,
		unrepresentable: e?.unrepresentable ?? "throw",
		override: e?.override ?? (() => {}),
		io: e?.io ?? "output",
		counter: 0,
		seen: /* @__PURE__ */ new Map(),
		sharedDefsExtractedFor: void 0,
		sharedEmitDoneFor: void 0,
		cycles: e?.cycles ?? "ref",
		reused: e?.reused ?? "inline",
		intersections: [],
		deferred: [],
		external: e?.external ?? void 0
	};
}
function Zo(e, t, n, r, i) {
	let a = typeof t.unrepresentable == "function" ? t.unrepresentable({
		zodSchema: e,
		path: r.path,
		message: i
	}) : t.unrepresentable;
	if (a === "any") return !1;
	if (a === void 0 || a === "throw") throw Error(i);
	return Object.assign(n, a), !0;
}
function q(e, t, n = {
	path: [],
	schemaPath: []
}) {
	var r;
	let i = e._zod.def, a = t.seen.get(e);
	if (a) return a.count++, n.schemaPath.includes(e) && (a.cycle = n.path), a.schema;
	let o = {
		schema: {},
		count: 1,
		cycle: void 0,
		path: n.path
	};
	t.seen.set(e, o), t.sharedDefsExtractedFor = void 0, t.sharedEmitDoneFor = void 0;
	let s = e._zod.toJSONSchema?.();
	if (s) o.schema = s;
	else {
		let r = {
			...n,
			schemaPath: [...n.schemaPath, e],
			path: n.path
		};
		if (e._zod.processJSONSchema) e._zod.processJSONSchema(t, o.schema, r);
		else {
			let n = o.schema, a = t.processors[i.type];
			if (!a) throw Error(`[toJSONSchema]: Non-representable type encountered: ${i.type}`);
			a(e, t, n, r);
		}
		let a = e._zod.parent;
		a && (o.ref ||= a, q(a, t, r), t.seen.get(a).isParent = !0);
	}
	let c = t.metadataRegistry.get(e);
	return c && Yo(o.schema, c), t.io === "input" && J(e) && (delete o.schema.examples, delete o.schema.default), t.io === "input" && "_prefault" in o.schema && ((r = o.schema).default ?? (r.default = o.schema._prefault)), delete o.schema._prefault, t.seen.get(e).schema;
}
function Qo(e) {
	return e.replace(/~/g, "~0").replace(/\//g, "~1");
}
function $o(e, t) {
	let n = e.seen.get(t);
	if (!n) throw Error("Unprocessed schema. This is a bug in Zod.");
	if (e.external && e.sharedDefsExtractedFor === e.external) return;
	let r = /* @__PURE__ */ new Map();
	for (let t of e.seen.entries()) {
		let n = e.metadataRegistry.get(t[0])?.id;
		if (n) {
			let e = r.get(n);
			if (e && e !== t[0]) throw Error(`Duplicate schema id "${n}" detected during JSON Schema conversion. Two different schemas cannot share the same id when converted together.`);
			r.set(n, t[0]);
		}
	}
	let i = (t) => {
		let r = e.target === "draft-2020-12" ? "$defs" : "definitions";
		if (e.external) {
			let n = e.external.registry.get(t[0])?.id, i = e.external.uri ?? ((e) => e);
			if (n) return { ref: i(n) };
			let a = t[1].defId ?? t[1].schema.id ?? `schema${e.counter++}`;
			return t[1].defId = a, {
				defId: a,
				ref: `${i("__shared")}#/${r}/${Qo(a)}`
			};
		}
		let i = `#/${r}/`;
		if (t[1] === n && !t[1].schema.id) return { ref: "#" };
		let a = t[1].schema.id ?? `__schema${e.counter++}`;
		return {
			defId: a,
			ref: i + Qo(a)
		};
	}, a = (e) => {
		if (e[1].schema.$ref) return;
		let t = e[1], { ref: n, defId: r } = i(e);
		t.def = { ...t.schema }, r && (t.defId = r);
		let a = t.schema;
		for (let e in a) delete a[e];
		a.$ref = n;
	};
	if (e.cycles === "throw") for (let t of e.seen.entries()) {
		let e = t[1];
		if (e.cycle) throw Error(`Cycle detected: #/${e.cycle?.join("/")}/<root>

Set the \`cycles\` parameter to \`"ref"\` to resolve cyclical schemas with defs.`);
	}
	for (let n of e.seen.entries()) {
		let r = n[1];
		if (t === n[0]) a(n);
		else {
			if (e.external) {
				let r = e.external.registry.get(n[0])?.id;
				if (t !== n[0] && r) {
					a(n);
					continue;
				}
			}
			(e.metadataRegistry.get(n[0])?.id || r.cycle || r.count > 1 && e.reused === "ref") && a(n);
		}
	}
	e.external && (e.sharedDefsExtractedFor = e.external);
}
function es(e) {
	let t = e.anyOf;
	if (!Array.isArray(t) || t.length === 0 || e.type !== void 0) return;
	let n = [];
	for (let e of t) {
		if (!e || typeof e != "object") return;
		es(e);
		let t = Object.keys(e);
		if (t.length !== 1 || t[0] !== "type") return;
		let r = e.type;
		for (let e of Array.isArray(r) ? r : [r]) {
			if (typeof e != "string") return;
			n.includes(e) || n.push(e);
		}
	}
	delete e.anyOf, e.type = n.length === 1 ? n[0] : n;
}
var ts = /* @__PURE__ */ new Set([
	"type",
	"properties",
	"required",
	"additionalProperties"
]), ns = ["oneOf", "anyOf"];
function rs(e) {
	let t = e.additionalProperties;
	return t === void 0 || t === !1 || typeof t != "object" || !t ? null : Object.keys(t).length ? t : null;
}
function is(e) {
	let t = [];
	for (let n of e) {
		if (typeof n != "object" || n.type !== "object") return null;
		for (let e in n) if (!ts.has(e)) return null;
		t.push(n);
	}
	let n = {}, r = /* @__PURE__ */ new Set();
	for (let e of t) {
		for (let r in e.properties) {
			if (Object.prototype.hasOwnProperty.call(n, r)) continue;
			let e = [];
			for (let n of t) {
				let t = n.properties?.[r] ?? rs(n);
				t != null && (e.some((e) => JSON.stringify(e) === JSON.stringify(t)) || e.push(t));
			}
			j(n, r, e.length === 1 ? e[0] : is(e) ?? { allOf: e });
		}
		for (let t of e.required ?? []) r.add(t);
	}
	let i = {
		type: "object",
		properties: n
	};
	if (r.size && (i.required = [...r]), t.every((e) => e.additionalProperties === !1)) i.additionalProperties = !1;
	else {
		let e = [];
		for (let n of t) {
			let t = rs(n);
			t && !e.some((e) => JSON.stringify(e) === JSON.stringify(t)) && e.push(t);
		}
		e.length === 1 ? i.additionalProperties = e[0] : e.length > 1 && (i.additionalProperties = { allOf: e });
	}
	return i;
}
function as(e) {
	let t = e.allOf;
	if (!Array.isArray(t) || t.length < 2) return;
	for (let t of ts) if (t in e) return;
	let n = t.filter((e) => ns.some((t) => Array.isArray(e[t]))), r = null;
	if (!n.length) r = is(t);
	else {
		let e = n[0], i = ns.find((t) => Array.isArray(e[t]));
		if (Object.keys(e).length !== 1) return;
		let a = t.filter((t) => t !== e), o = e[i].map((e) => is([...a, e]));
		if (o.some((e) => !e)) return;
		r = { [i]: o };
	}
	r && (delete e.allOf, Yo(e, r));
}
function os(e, t) {
	let n = e.seen.get(t);
	if (!n) throw Error("Unprocessed schema. This is a bug in Zod.");
	let r = (t) => {
		let n = e.seen.get(t);
		if (n.ref === null) return;
		let i = n.def ?? n.schema, a = { ...i }, o = n.ref;
		if (n.ref = null, o) {
			r(o);
			let n = e.seen.get(o), s = n.schema;
			if (s.$ref && (e.target === "draft-07" || e.target === "draft-04" || e.target === "openapi-3.0") ? (i.allOf = i.allOf ?? [], i.allOf.push(s)) : Yo(i, s), Yo(i, a), t._zod.parent === o) for (let e in i) e !== "$ref" && e !== "allOf" && (e in a || delete i[e]);
			if (s.$ref && n.def) for (let e in i) e !== "$ref" && e !== "allOf" && e in n.def && JSON.stringify(i[e]) === JSON.stringify(n.def[e]) && delete i[e];
		}
		let s = t._zod.parent;
		if (s && s !== o) {
			r(s);
			let t = e.seen.get(s);
			if (t?.schema.$ref && (i.$ref = t.schema.$ref, t.def)) for (let e in i) e !== "$ref" && e !== "allOf" && e in t.def && JSON.stringify(i[e]) === JSON.stringify(t.def[e]) && delete i[e];
		}
		e.override({
			zodSchema: t,
			jsonSchema: i,
			path: n.path ?? []
		});
	};
	if (!e.external || e.sharedEmitDoneFor !== e.external) {
		for (let t of [...e.seen.entries()].reverse()) r(t[0]);
		if (e.target !== "openapi-3.0") for (let t of e.seen.entries()) es(t[1].def ?? t[1].schema);
		for (let t of e.deferred) t();
		if (e.intersections.length) {
			let t = /* @__PURE__ */ new Map();
			for (let n of e.seen.values()) for (let e of [n.schema, n.def]) {
				let n = e?.allOf;
				if (!Array.isArray(n)) continue;
				let r = t.get(n);
				r ? r.push(e) : t.set(n, [e]);
			}
			for (let n of e.intersections) for (let e of t.get(n) ?? []) as(e);
		}
	}
	let i = {};
	if (e.target === "draft-2020-12" ? i.$schema = "https://json-schema.org/draft/2020-12/schema" : e.target === "draft-07" ? i.$schema = "http://json-schema.org/draft-07/schema#" : e.target === "draft-04" ? i.$schema = "http://json-schema.org/draft-04/schema#" : e.target, e.external?.uri) {
		let n = e.external.registry.get(t)?.id;
		if (!n) throw Error("Schema is missing an `id` property");
		i.$id = e.external.uri(n);
	}
	Yo(i, n.defId ? n.schema : n.def ?? n.schema);
	let a = e.metadataRegistry.get(t)?.id;
	a !== void 0 && i.id === a && delete i.id;
	let o = e.external?.defs ?? {};
	if (!e.external || e.sharedEmitDoneFor !== e.external) for (let t of e.seen.entries()) {
		let e = t[1];
		e.def && e.defId && (e.def.id === e.defId && delete e.def.id, j(o, e.defId, e.def));
	}
	e.external && (e.sharedEmitDoneFor = e.external), e.external || Object.keys(o).length > 0 && (e.target === "draft-2020-12" ? i.$defs = o : i.definitions = o);
	try {
		let n = JSON.parse(JSON.stringify(i));
		return Object.defineProperty(n, "~standard", {
			value: {
				...t["~standard"],
				jsonSchema: {
					input: cs(t, "input", e.processors),
					output: cs(t, "output", e.processors)
				}
			},
			enumerable: !1,
			writable: !1
		}), n;
	} catch {
		throw Error("Error converting schema to JSON.");
	}
}
function J(e, t) {
	let n = t ?? { seen: /* @__PURE__ */ new Set() };
	if (n.seen.has(e)) return !1;
	n.seen.add(e);
	let r = e._zod.def;
	if (r.type === "transform") return !0;
	if (r.type === "array") return J(r.element, n);
	if (r.type === "set") return J(r.valueType, n);
	if (r.type === "lazy") return J(r.getter(), n);
	if (r.type === "promise" || r.type === "optional" || r.type === "nonoptional" || r.type === "nullable" || r.type === "readonly" || r.type === "default" || r.type === "prefault" || r.type === "catch") return J(r.innerType, n);
	if (r.type === "intersection") return J(r.left, n) || J(r.right, n);
	if (r.type === "record" || r.type === "map") return J(r.keyType, n) || J(r.valueType, n);
	if (r.type === "pipe") return e._zod.traits.has("$ZodCodec") ? !0 : J(r.in, n) || J(r.out, n);
	if (r.type === "object") {
		for (let e in r.shape) if (J(r.shape[e], n)) return !0;
		return !1;
	}
	if (r.type === "union") {
		for (let e of r.options) if (J(e, n)) return !0;
		return !1;
	}
	if (r.type === "tuple") {
		for (let e of r.items) if (J(e, n)) return !0;
		return !!(r.rest && J(r.rest, n));
	}
	return !1;
}
var ss = (e, t = {}) => (n) => {
	let r = Xo({
		...n,
		processors: t
	});
	return q(e, r), $o(r, e), os(r, e);
}, cs = (e, t, n = {}) => (r) => {
	let { libraryOptions: i, target: a } = r ?? {}, o = Xo({
		...i ?? {},
		target: a,
		io: t,
		processors: n
	});
	return q(e, o), $o(o, e), os(o, e);
}, ls = (e, t, n) => {
	(e[t] === void 0 || n > e[t]) && (e[t] = n);
}, us = (e, t, n) => {
	(e[t] === void 0 || n < e[t]) && (e[t] = n);
}, ds = (e, t) => {
	ls(e, "minimum", t), us(e, "maximum", t);
}, fs = (e, t) => {
	e.multipleOf ??= [], e.multipleOf.includes(t) || e.multipleOf.push(t);
}, ps = (e, t) => {
	e.patterns ??= /* @__PURE__ */ new Set(), e.patterns.add(t);
}, ms = (e, t) => {
	e.mime = e.mime ? e.mime.filter((e) => t.includes(e)) : [...t];
}, hs = (e, t) => {
	e.format = t, t.includes("int") && (e.isInt = !0);
}, gs = (e, t) => ls(e, "minimum", t.minimum), _s = (e, t) => us(e, "maximum", t.maximum), vs = (e) => (t, n) => {
	hs(t, n.format);
	let [r, i] = e[n.format];
	ls(t, "minimum", r), us(t, "maximum", i);
}, ys = {
	greater_than: (e, t) => ls(e, t.inclusive ? "minimum" : "exclusiveMinimum", t.value),
	less_than: (e, t) => us(e, t.inclusive ? "maximum" : "exclusiveMaximum", t.value),
	multiple_of: (e, t) => fs(e, t.value),
	number_format: vs($t),
	bigint_format: vs(en),
	min_length: gs,
	max_length: _s,
	length_equals: (e, t) => ds(e, t.length),
	min_size: gs,
	max_size: _s,
	size_equals: (e, t) => ds(e, t.size),
	string_format: (e, t) => {
		hs(e, t.format), t.pattern && ps(e, t.pattern), (t.format === "base64" || t.format === "base64url") && (e.contentEncoding = t.format), (t.local || t.precision === -1) && (e.laxFormat = !0);
	},
	mime_type: (e, t) => ms(e, t.mime)
};
function bs(e) {
	let t = {}, n = e._zod.def, r = e._zod.traits.has("$ZodCheck") ? [e, ...n.checks ?? []] : n.checks ?? [];
	for (let e of r) ys[e._zod.def.check]?.(t, e._zod.def);
	let i = e._zod.bag;
	i.minimum !== void 0 && ls(t, "minimum", i.minimum), i.exclusiveMinimum !== void 0 && ls(t, "exclusiveMinimum", i.exclusiveMinimum), i.maximum !== void 0 && us(t, "maximum", i.maximum), i.exclusiveMaximum !== void 0 && us(t, "exclusiveMaximum", i.exclusiveMaximum), i.multipleOf !== void 0 && fs(t, i.multipleOf), i.format !== void 0 && (t.format ??= i.format, i.format.includes("int") && (t.isInt = !0)), i.mime && ms(t, i.mime);
	for (let e of i.patterns ?? []) ps(t, e);
	return t;
}
var xs = {
	guid: "uuid",
	url: "uri",
	datetime: "date-time",
	json_string: "json-string",
	regex: ""
}, Ss = /* @__PURE__ */ new Map([[Ri, Or], [Bi, kr]]), Cs = (e) => Ss.get(e) ?? e, ws = (e, t, n, r) => {
	let i = n;
	i.type = "string";
	let { minimum: a, maximum: o, format: s, patterns: c, contentEncoding: l, laxFormat: u } = bs(e);
	if (typeof a == "number" && (i.minLength = a), typeof o == "number" && (i.maxLength = o), s && (i.format = xs[s] ?? s, i.format === "" && delete i.format, (s === "time" || u) && delete i.format), l && (i.contentEncoding = l), c && c.size > 0) {
		let e = [...c].map(Cs);
		e.length === 1 ? i.pattern = e[0].source : e.length > 1 && (i.allOf = [...e.map((e) => ({
			...t.target === "draft-07" || t.target === "draft-04" || t.target === "openapi-3.0" ? { type: "string" } : {},
			pattern: e.source
		}))]);
	}
}, Ts = (e, t, n, r) => {
	n.type = "boolean";
}, Es = (e, t, n, r) => {
	n.not = {};
}, Ds = (e, t, n, r) => {
	let i = e._zod.def, a = At(i.entries);
	a.length === 0 ? n.not = {} : (a.every((e) => typeof e == "number") && (n.type = "number"), a.every((e) => typeof e == "string") && (n.type = "string"), n.enum = a);
}, Os = (e, t, n, r) => {
	let i = e._zod.def;
	if (i.values.length === 0) {
		n.not = {};
		return;
	}
	let a = [];
	for (let o of i.values) if (o === void 0) {
		if (Zo(e, t, n, r, "Literal `undefined` cannot be represented in JSON Schema")) return;
	} else if (typeof o == "bigint") {
		if (Zo(e, t, n, r, "BigInt literals cannot be represented in JSON Schema")) return;
		a.push(Number(o));
	} else a.push(o);
	if (a.length !== 0) {
		if (a.length === 1) {
			let e = a[0];
			n.type = e === null ? "null" : typeof e, t.target === "draft-04" || t.target === "openapi-3.0" ? n.enum = [e] : n.const = e;
		} else a.every((e) => typeof e == "number") && (n.type = "number"), a.every((e) => typeof e == "string") && (n.type = "string"), a.every((e) => typeof e == "boolean") && (n.type = "boolean"), a.every((e) => e === null) && (n.type = "null"), n.enum = a;
	}
}, ks = (e, t, n, r) => {
	Zo(e, t, n, r, "Custom types cannot be represented in JSON Schema");
}, As = (e, t, n, r) => {
	Zo(e, t, n, r, "Transforms cannot be represented in JSON Schema");
}, js = (e, t, n, r) => {
	let i = n, a = e._zod.def, { minimum: o, maximum: s } = bs(e);
	typeof o == "number" && (i.minItems = o), typeof s == "number" && (i.maxItems = s), i.type = "array", i.items = q(a.element, t, {
		...r,
		path: [...r.path, "items"]
	});
};
function Ms(e) {
	let t = e._zod.def;
	return t.type === "pipe" && t.in._zod.traits.has("$ZodTransform") ? Ms(t.out) : t.type === "catch" ? Ms(t.innerType) : e._zod.optin;
}
var Ns = (e, t, n, r) => {
	let i = n, a = e._zod.def, o = a.shape;
	if (Object.getOwnPropertySymbols(o).length && Zo(e, t, i, r, "Symbol keys cannot be represented in JSON Schema")) return;
	i.type = "object", i.properties = {};
	for (let e in o) j(i.properties, e, q(o[e], t, {
		...r,
		path: [
			...r.path,
			"properties",
			e
		]
	}));
	let s = [];
	for (let e of Object.keys(o)) {
		let n = a.shape[e];
		(t.io === "input" ? Ms(n) === void 0 : n._zod.optout === void 0) && s.push(e);
	}
	s.length > 0 && (i.required = s), a.catchall?._zod.def.type === "never" ? i.additionalProperties = !1 : a.catchall ? a.catchall && (i.additionalProperties = q(a.catchall, t, {
		...r,
		path: [...r.path, "additionalProperties"]
	})) : t.io === "output" && (i.additionalProperties = !1);
}, Ps = (e, t, n, r) => {
	let i = e._zod.def, a = i.inclusive === !1, o = i.options.map((e, n) => q(e, t, {
		...r,
		path: [
			...r.path,
			a ? "oneOf" : "anyOf",
			n
		]
	}));
	a ? n.oneOf = o : n.anyOf = o;
}, Fs = (e, t, n, r) => {
	let i = e._zod.def, a = q(i.left, t, {
		...r,
		path: [
			...r.path,
			"allOf",
			0
		]
	}), o = q(i.right, t, {
		...r,
		path: [
			...r.path,
			"allOf",
			1
		]
	}), s = (e) => "allOf" in e && Object.keys(e).length === 1, c = [...s(a) ? a.allOf : [a], ...s(o) ? o.allOf : [o]];
	n.allOf = c, t.intersections.push(c);
};
function Is(e, t, n) {
	if (t.$ref) {
		if (n.has(t)) return t;
		n.add(t);
		let r = e.get(t)?.def;
		if (!r) return t;
		let i = Is(e, r, n);
		return i === r ? t : i;
	}
	for (let r of ["anyOf", "oneOf"]) {
		let i = t[r];
		if (!Array.isArray(i)) continue;
		let a = i.map((t) => Is(e, t, n));
		a.some((e, t) => e !== i[t]) && (t = {
			...t,
			[r]: a
		});
	}
	let r = Array.isArray(t.type) ? t.type : [t.type], i = !r.includes("string") && r.some((e) => e === "number" || e === "integer"), a = t.enum ?? (t.const === void 0 ? void 0 : [t.const]);
	if (!i && !a?.some((e) => typeof e == "number")) return t;
	let { minimum: o, maximum: s, exclusiveMinimum: c, exclusiveMaximum: l, multipleOf: u, format: d, id: f, ...p } = t;
	return p.enum ? p.enum = p.enum.map((e) => typeof e == "number" ? String(e) : e) : typeof p.const == "number" && (p.const = String(p.const)), i && (p.type = "string", a || (p.pattern = (r.includes("number") ? Br : zr).source)), p;
}
var Ls = /* @__PURE__ */ new WeakMap();
function Rs(e) {
	let t = /* @__PURE__ */ new Map();
	for (let n of e.seen.values()) n.def && !t.has(n.schema) && t.set(n.schema, n);
	let n = /* @__PURE__ */ new Map();
	for (let r of Ls.get(e) ?? []) {
		let i = e.seen.get(r), a = (i?.def ?? i?.schema)?.propertyNames;
		if (!a || a === !0 || n.has(a)) continue;
		let o = Is(t, a, /* @__PURE__ */ new Set());
		o !== a && n.set(a, o);
	}
	if (n.size) for (let t of e.seen.values()) for (let e of [t.schema, t.def]) {
		let t = e && n.get(e.propertyNames);
		t && (e.propertyNames = t);
	}
}
var zs = (e, t, n, r) => {
	let i = n, a = e._zod.def;
	i.type = "object";
	let o = a.keyType, s = bs(o).patterns;
	if (a.mode === "loose" && s && s.size > 0) {
		let e = q(a.valueType, t, {
			...r,
			path: [
				...r.path,
				"patternProperties",
				"*"
			]
		});
		i.patternProperties = {};
		for (let t of s) j(i.patternProperties, Cs(t).source, e);
	} else {
		if (t.target === "draft-07" || t.target === "draft-2020-12") {
			i.propertyNames = q(a.keyType, t, {
				...r,
				path: [...r.path, "propertyNames"]
			});
			let n = Ls.get(t);
			n || (n = [], Ls.set(t, n), t.deferred.push(() => Rs(t))), n.push(e);
		}
		i.additionalProperties = q(a.valueType, t, {
			...r,
			path: [...r.path, "additionalProperties"]
		});
	}
	let c = o._zod.values, l = t.io === "input" && Ms(a.valueType) !== void 0;
	if (c && !a.partial && !l) {
		let e = [...c].filter((e) => typeof e == "string" || typeof e == "number");
		e.length > 0 && (i.required = e.map(String));
	}
}, Bs = (e, t, n, r) => {
	let i = e._zod.def, a = q(i.innerType, t, r), o = t.seen.get(e);
	t.target === "openapi-3.0" ? (o.ref = i.innerType, n.nullable = !0) : n.anyOf = [a, { type: "null" }];
}, Vs = (e, t, n, r) => {
	let i = e._zod.def;
	q(i.innerType, t, r);
	let a = t.seen.get(e);
	a.ref = i.innerType;
}, Hs = Symbol();
function Us(e, t, n, r, i) {
	let a = !1, o = JSON.stringify(e, (e, t) => typeof t == "bigint" ? (a = !0, null) : t);
	return a ? (Zo(t, n, r, i, "BigInt defaults cannot be represented in JSON Schema"), Hs) : JSON.parse(o);
}
var Ws = (e, t, n, r) => {
	let i = e._zod.def;
	q(i.innerType, t, r);
	let a = t.seen.get(e);
	a.ref = i.innerType;
	let o = Us(i.defaultValue, e, t, n, r);
	o !== Hs && (n.default = o);
}, Gs = (e, t, n, r) => {
	let i = e._zod.def;
	q(i.innerType, t, r);
	let a = t.seen.get(e);
	if (a.ref = i.innerType, t.io !== "input") return;
	let o = Us(i.defaultValue, e, t, n, r);
	o !== Hs && (n._prefault = o);
}, Ks = (e, t, n, r) => {
	let i = e._zod.def;
	q(i.innerType, t, r);
	let a = t.seen.get(e);
	a.ref = i.innerType;
	let o;
	try {
		o = i.catchValue(void 0);
	} catch {
		Zo(e, t, n, r, "Dynamic catch values are not supported in JSON Schema");
		return;
	}
	n.default = o;
}, qs = (e, t, n, r) => {
	let i = e._zod.def, a = i.in._zod.traits.has("$ZodTransform"), o = t.io === "input" ? a ? i.out : i.in : i.out;
	q(o, t, r);
	let s = t.seen.get(e);
	s.ref = o;
}, Js = (e, t, n, r) => {
	let i = e._zod.def;
	q(i.innerType, t, r);
	let a = t.seen.get(e);
	a.ref = i.innerType, n.readOnly = !0;
}, Ys = (e, t, n, r) => {
	let i = e._zod.def;
	q(i.innerType, t, r);
	let a = t.seen.get(e);
	a.ref = i.innerType;
}, Xs = /* @__PURE__ */ new WeakSet([Object.prototype, Error.prototype]);
function Zs(e, t, n) {
	Object.defineProperty(e, t, {
		configurable: !0,
		enumerable: !1,
		get() {
			let e = n(this);
			return Object.defineProperty(this, t, {
				value: e,
				configurable: !0,
				writable: !0
			}), e;
		},
		set(e) {
			Object.defineProperty(this, t, {
				value: e,
				configurable: !0,
				writable: !0
			});
		}
	});
}
var Y = /*@__PURE__*/ B("ZodError", (e, t) => {
	Hn.init(e, t), e.name = "ZodError";
	let n = Object.getPrototypeOf(e);
	Xs.has(n) || (Xs.add(n), Zs(n, "format", (e) => (t) => Gn(e, t)), Zs(n, "flatten", (e) => (t) => Wn(e, t)), Zs(n, "addIssue", (e) => (t) => {
		e.issues.push(t), e.message = JSON.stringify(e.issues, Mt, 2);
	}), Zs(n, "addIssues", (e) => (t) => {
		e.issues.push(...t), e.message = JSON.stringify(e.issues, Mt, 2);
	}), Object.defineProperty(n, "isEmpty", {
		configurable: !0,
		enumerable: !1,
		get() {
			return this.issues.length === 0;
		}
	}));
}, void 0, { Parent: Error }), Qs = /* @__PURE__ */ qn(Y), $s = /* @__PURE__ */ Jn(Y), ec = /* @__PURE__ */ Yn(Y), tc = /* @__PURE__ */ Zn(Y), nc = /* @__PURE__ */ rr(Y), rc = /* @__PURE__ */ ir(Y), ic = /* @__PURE__ */ ar(Y), ac = /* @__PURE__ */ or(Y), oc = /* @__PURE__ */ sr(Y), sc = /* @__PURE__ */ cr(Y), cc = /* @__PURE__ */ lr(Y), lc = /* @__PURE__ */ ur(Y);
//#endregion
//#region node_modules/.pnpm/zod@4.6.5/node_modules/zod/v4/classic/schemas.js
function uc() {
	H.localeError || U(qa());
}
function dc() {
	H.memoizer || U({ memoizer: Wa() });
}
var X = /*@__PURE__*/ B("ZodType", (e, t) => (uc(), G.init(e, t), e.def = t, e.type = t.type, e), {
	check(...e) {
		let t = this.def;
		return this.clone(N(t, { checks: [...t.checks ?? [], ...e.map((e) => typeof e == "function" ? { _zod: {
			check: e,
			def: { check: "custom" },
			onattach: []
		} } : e)] }), { parent: !0 });
	},
	with(...e) {
		return this.check(...e);
	},
	clone(e, t) {
		return P(this, e, t);
	},
	brand() {
		return this;
	},
	register(e, t) {
		return e.add(this, t), this;
	},
	refine(e, t) {
		return this.check(Cl(e, t));
	},
	superRefine(e, t) {
		return this.check(wl(e, t));
	},
	overwrite(e) {
		return this.check(/* @__PURE__ */ zo(e));
	},
	optional() {
		return al(this);
	},
	exactOptional() {
		return sl(this);
	},
	nullable() {
		return ll(this);
	},
	nullish() {
		return al(ll(this));
	},
	nonoptional(e) {
		return hl(this, e);
	},
	array() {
		return Wc(this);
	},
	or(e) {
		return qc([this, e]);
	},
	and(e) {
		return Yc(this, e);
	},
	transform(e) {
		return yl(this, rl(e));
	},
	default(e) {
		return dl(this, e);
	},
	prefault(e) {
		return pl(this, e);
	},
	catch(e) {
		return _l(this, e);
	},
	pipe(e) {
		return yl(this, e);
	},
	readonly() {
		return xl(this);
	},
	describe(e) {
		let t = this.clone();
		return Za.add(t, { description: e }), t;
	},
	meta(...e) {
		if (e.length === 0) return Za.get(this);
		let t = this.clone();
		return Za.add(t, e[0]), t;
	},
	isOptional() {
		return this.safeParse(void 0).success;
	},
	isNullable() {
		return this.safeParse(null).success;
	},
	apply(e, ...t) {
		return t.length === 0 ? e(this) : e(this, ...t);
	},
	get "~standard"() {
		return xn(this, "~standard", {
			...oi(this),
			jsonSchema: {
				input: cs(this, "input"),
				output: cs(this, "output")
			}
		});
	},
	set "~standard"(e) {
		R(this, "~standard", e);
	},
	parse: function e(t, n) {
		return Qs(this, t, n, { callee: e });
	},
	parseAsync: async function e(t, n) {
		return await $s(this, t, n, { callee: e });
	},
	safeParse(e, t) {
		return ec(this, e, t);
	},
	async safeParseAsync(e, t) {
		return tc(this, e, t);
	},
	get spa() {
		return this?.safeParseAsync;
	},
	set spa(e) {
		R(this, "spa", e);
	},
	validate(e, t) {
		return er(this, e, t);
	},
	validateAsync(e, t) {
		return nr(this, e, t);
	},
	encode: function e(t, n) {
		return nc(this, t, n, { callee: e });
	},
	decode: function e(t, n) {
		return rc(this, t, n, { callee: e });
	},
	encodeAsync: async function e(t, n) {
		return await ic(this, t, n, { callee: e });
	},
	decodeAsync: async function e(t, n) {
		return await ac(this, t, n, { callee: e });
	},
	safeEncode(e, t) {
		return oc(this, e, t);
	},
	safeDecode(e, t) {
		return sc(this, e, t);
	},
	async safeEncodeAsync(e, t) {
		return cc(this, e, t);
	},
	async safeDecodeAsync(e, t) {
		return lc(this, e, t);
	},
	toJSONSchema(e) {
		return ss(this, {})(e);
	},
	get description() {
		return Za.get(this)?.description;
	},
	get _def() {
		return this._zod.def;
	}
}), fc = /*@__PURE__*/ B("_ZodString", (e, t) => {
	si.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => ws(e, t, n, r);
}, /*@__PURE__*/ Sn({
	format: (e) => bs(e).format ?? null,
	minLength: (e) => bs(e).minimum ?? null,
	maxLength: (e) => bs(e).maximum ?? null
}, {
	regex(...e) {
		return this.check(/* @__PURE__ */ No(...e));
	},
	includes(...e) {
		return this.check(/* @__PURE__ */ Io(...e));
	},
	startsWith(...e) {
		return this.check(/* @__PURE__ */ Lo(...e));
	},
	endsWith(...e) {
		return this.check(/* @__PURE__ */ Ro(...e));
	},
	min(...e) {
		return this.check(/* @__PURE__ */ jo(...e));
	},
	max(...e) {
		return this.check(/* @__PURE__ */ Ao(...e));
	},
	length(...e) {
		return this.check(/* @__PURE__ */ Mo(...e));
	},
	nonempty(...e) {
		return this.check(/* @__PURE__ */ jo(1, ...e));
	},
	lowercase(e) {
		return this.check(/* @__PURE__ */ Po(e));
	},
	uppercase(e) {
		return this.check(/* @__PURE__ */ Fo(e));
	},
	trim() {
		return this.check(/* @__PURE__ */ Vo());
	},
	normalize(...e) {
		return this.check(/* @__PURE__ */ Bo(...e));
	},
	toLowerCase() {
		return this.check(/* @__PURE__ */ Ho());
	},
	toUpperCase() {
		return this.check(/* @__PURE__ */ Uo());
	},
	slugify() {
		return this.check(/* @__PURE__ */ Wo());
	}
})), pc = /*@__PURE__*/ B("ZodString", (e, t) => {
	si.init(e, t), fc.init(e, t);
}, {
	email(e) {
		return this.check(/* @__PURE__ */ eo(vc, e));
	},
	url(e) {
		return this.check(/* @__PURE__ */ oo(xc, e));
	},
	jwt(e) {
		return this.check(/* @__PURE__ */ So(Ic, e));
	},
	emoji(e) {
		return this.check(/* @__PURE__ */ so(Sc, e));
	},
	guid(e) {
		return this.check(/* @__PURE__ */ to(yc, e));
	},
	uuid(e) {
		return this.check(/* @__PURE__ */ no(bc, e));
	},
	uuidv4(e) {
		return this.check(/* @__PURE__ */ ro(bc, e));
	},
	uuidv6(e) {
		return this.check(/* @__PURE__ */ io(bc, e));
	},
	uuidv7(e) {
		return this.check(/* @__PURE__ */ ao(bc, e));
	},
	nanoid(e) {
		return this.check(/* @__PURE__ */ co(Cc, e));
	},
	cuid(e) {
		return this.check(/* @__PURE__ */ lo(wc, e));
	},
	cuid2(e) {
		return this.check(/* @__PURE__ */ uo(Tc, e));
	},
	ulid(e) {
		return this.check(/* @__PURE__ */ fo(Ec, e));
	},
	base64(e) {
		return this.check(/* @__PURE__ */ yo(Nc, e));
	},
	base64url(e) {
		return this.check(/* @__PURE__ */ bo(Pc, e));
	},
	xid(e) {
		return this.check(/* @__PURE__ */ po(Dc, e));
	},
	ksuid(e) {
		return this.check(/* @__PURE__ */ mo(Oc, e));
	},
	ipv4(e) {
		return this.check(/* @__PURE__ */ ho(kc, e));
	},
	ipv6(e) {
		return this.check(/* @__PURE__ */ go(Ac, e));
	},
	cidrv4(e) {
		return this.check(/* @__PURE__ */ _o(jc, e));
	},
	cidrv6(e) {
		return this.check(/* @__PURE__ */ vo(Mc, e));
	},
	e164(e) {
		return this.check(/* @__PURE__ */ xo(Fc, e));
	},
	datetime(e) {
		return this.check(/* @__PURE__ */ Co(mc, e));
	},
	date(e) {
		return this.check(/* @__PURE__ */ wo(hc, e));
	},
	time(e) {
		return this.check(/* @__PURE__ */ To(gc, e));
	},
	duration(e) {
		return this.check(/* @__PURE__ */ Eo(_c, e));
	}
});
function Z(e) {
	return /* @__PURE__ */ $a(pc, e);
}
var Q = /*@__PURE__*/ B("ZodStringFormat", (e, t) => {
	K.init(e, t), fc.init(e, t);
}), mc = /*@__PURE__*/ B("ZodISODateTime", (e, t) => {
	Ei.init(e, t), Q.init(e, t);
}), hc = /*@__PURE__*/ B("ZodISODate", (e, t) => {
	Di.init(e, t), Q.init(e, t);
}), gc = /*@__PURE__*/ B("ZodISOTime", (e, t) => {
	Oi.init(e, t), Q.init(e, t);
}), _c = /*@__PURE__*/ B("ZodISODuration", (e, t) => {
	ki.init(e, t), Q.init(e, t);
}), vc = /*@__PURE__*/ B("ZodEmail", (e, t) => {
	ui.init(e, t), Q.init(e, t);
}), yc = /*@__PURE__*/ B("ZodGUID", (e, t) => {
	ci.init(e, t), Q.init(e, t);
}), bc = /*@__PURE__*/ B("ZodUUID", (e, t) => {
	li.init(e, t), Q.init(e, t);
}), xc = /*@__PURE__*/ B("ZodURL", (e, t) => {
	vi.init(e, t), Q.init(e, t);
}), Sc = /*@__PURE__*/ B("ZodEmoji", (e, t) => {
	yi.init(e, t), Q.init(e, t);
}), Cc = /*@__PURE__*/ B("ZodNanoID", (e, t) => {
	bi.init(e, t), Q.init(e, t);
}), wc = /*@__PURE__*/ B("ZodCUID", (e, t) => {
	xi.init(e, t), Q.init(e, t);
}), Tc = /*@__PURE__*/ B("ZodCUID2", (e, t) => {
	Si.init(e, t), Q.init(e, t);
}), Ec = /*@__PURE__*/ B("ZodULID", (e, t) => {
	Ci.init(e, t), Q.init(e, t);
}), Dc = /*@__PURE__*/ B("ZodXID", (e, t) => {
	wi.init(e, t), Q.init(e, t);
}), Oc = /*@__PURE__*/ B("ZodKSUID", (e, t) => {
	Ti.init(e, t), Q.init(e, t);
}), kc = /*@__PURE__*/ B("ZodIPv4", (e, t) => {
	Ai.init(e, t), Q.init(e, t);
}), Ac = /*@__PURE__*/ B("ZodIPv6", (e, t) => {
	Ni.init(e, t), Q.init(e, t);
}), jc = /*@__PURE__*/ B("ZodCIDRv4", (e, t) => {
	Pi.init(e, t), Q.init(e, t);
}), Mc = /*@__PURE__*/ B("ZodCIDRv6", (e, t) => {
	Ii.init(e, t), Q.init(e, t);
}), Nc = /*@__PURE__*/ B("ZodBase64", (e, t) => {
	zi.init(e, t), Q.init(e, t);
}), Pc = /*@__PURE__*/ B("ZodBase64URL", (e, t) => {
	Hi.init(e, t), Q.init(e, t);
}), Fc = /*@__PURE__*/ B("ZodE164", (e, t) => {
	Ui.init(e, t), Q.init(e, t);
}), Ic = /*@__PURE__*/ B("ZodJWT", (e, t) => {
	Gi.init(e, t), Q.init(e, t);
}), Lc = /*@__PURE__*/ B("ZodBoolean", (e, t) => {
	Ki.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ts(e, t, n, r);
});
function Rc(e) {
	return /* @__PURE__ */ Do(Lc, e);
}
var zc = /*@__PURE__*/ B("ZodUnknown", (e, t) => {
	qi.init(e, t), X.init(e, t), e._zod.processJSONSchema = (e, t, n) => void 0;
});
function Bc() {
	return /* @__PURE__ */ Oo(zc);
}
var Vc = /*@__PURE__*/ B("ZodNever", (e, t) => {
	Ji.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Es(e, t, n, r);
});
function Hc(e) {
	return /* @__PURE__ */ ko(Vc, e);
}
var Uc = /*@__PURE__*/ B("ZodArray", (e, t) => {
	dc(), Xi.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => js(e, t, n, r), e.element = t.element;
}, {
	min(e, t) {
		return this.check(/* @__PURE__ */ jo(e, t));
	},
	nonempty(e) {
		return this.check(/* @__PURE__ */ jo(1, e));
	},
	max(e, t) {
		return this.check(/* @__PURE__ */ Ao(e, t));
	},
	length(e, t) {
		return this.check(/* @__PURE__ */ Mo(e, t));
	},
	unwrap() {
		return this.element;
	}
});
function Wc(e, t) {
	return /* @__PURE__ */ Go(Uc, e, t);
}
var Gc = /*@__PURE__*/ B("ZodObject", (e, t) => {
	dc(), na.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ns(e, t, n, r), On(e, "shape", (e) => e._zod.def.shape, !1);
}, {
	keyof() {
		return $c(Object.keys(this._zod.def.shape));
	},
	catchall(e) {
		return this.clone(N(this._zod.def, { catchall: e }));
	},
	passthrough() {
		return this.clone(N(this._zod.def, { catchall: Bc() }));
	},
	loose() {
		return this.clone(N(this._zod.def, { catchall: Bc() }));
	},
	strict() {
		return this.clone(N(this._zod.def, { catchall: Hc() }));
	},
	strip() {
		return this.clone(N(this._zod.def, { catchall: void 0 }));
	},
	extend(e) {
		return an(this, e);
	},
	safeExtend(e) {
		return sn(this, e);
	},
	merge(e) {
		return cn(this, e);
	},
	pick(e) {
		return tn(this, e);
	},
	omit(e) {
		return rn(this, e);
	},
	partial(...e) {
		return ln(il, this, e[0]);
	},
	exactPartial(...e) {
		return ln(ol, this, e[0], "exactPartial");
	},
	required(...e) {
		return un(ml, this, e[0]);
	}
});
function $(e, t) {
	return new Gc({
		type: "object",
		shape: e ?? {},
		...F(t)
	});
}
var Kc = /*@__PURE__*/ B("ZodUnion", (e, t) => {
	ia.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ps(e, t, n, r), e.options = t.options;
});
function qc(e, t) {
	return new Kc({
		type: "union",
		options: e,
		...F(t)
	});
}
var Jc = /*@__PURE__*/ B("ZodIntersection", (e, t) => {
	aa.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Fs(e, t, n, r);
});
function Yc(e, t) {
	return new Jc({
		type: "intersection",
		left: e,
		right: t
	});
}
var Xc = /*@__PURE__*/ B("ZodRecord", (e, t) => {
	dc(), ca.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => zs(e, t, n, r), e.keyType = t.keyType, e.valueType = t.valueType;
});
function Zc(e, t, n) {
	return new Xc({
		type: "record",
		keyType: e,
		valueType: t,
		...F(n),
		partial: !0
	});
}
var Qc = /*@__PURE__*/ B("ZodEnum", (e, t) => {
	la.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ds(e, t, n, r), e.enum = t.entries, e.options = [...e._zod.values];
	let n = new Set(Object.keys(t.entries));
	e.extract = (e, r) => {
		let i = {};
		for (let r of e) if (n.has(r)) i[r] = t.entries[r];
		else throw Error(`Key ${r} not found in enum`);
		return new Qc({
			...t,
			checks: [],
			...F(r),
			entries: i
		});
	}, e.exclude = (e, r) => {
		let i = { ...t.entries };
		for (let t of e) if (n.has(t)) delete i[t];
		else throw Error(`Key ${t} not found in enum`);
		return new Qc({
			...t,
			checks: [],
			...F(r),
			entries: i
		});
	};
});
function $c(e, t) {
	return new Qc({
		type: "enum",
		entries: Array.isArray(e) ? Object.fromEntries(e.map((e) => [e, e])) : e,
		...F(t)
	});
}
var el = /*@__PURE__*/ B("ZodLiteral", (e, t) => {
	ua.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Os(e, t, n, r), e.values = new Set(t.values), Object.defineProperty(e, "value", { get() {
		if (t.values.length > 1) throw Error("This schema contains multiple valid literal values. Use `.values` instead.");
		return t.values[0];
	} });
});
function tl(e, t) {
	return new el({
		type: "literal",
		values: Array.isArray(e) ? e : [e],
		...F(t)
	});
}
var nl = /*@__PURE__*/ B("ZodTransform", (e, t) => {
	dc(), da.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => As(e, t, n, r), e._zod.parse = (n, r) => {
		if (r.direction === "backward") throw new Fn(e.constructor.name);
		n.addIssue = (r) => {
			if (typeof r == "string") n.issues.push(yn(r, n.value, t));
			else {
				let t = r;
				t.fatal && (t.continue = !1), t.code ??= "custom", "input" in t || (t.input = n.value), t.inst ??= e, n.issues.push(yn(t));
			}
		};
		let i = t.transform(n.value, n);
		return i instanceof Promise ? i.then((e) => (n.value = e, n)) : (n.value = i, n);
	};
});
function rl(e) {
	return new nl({
		type: "transform",
		transform: e
	});
}
var il = /*@__PURE__*/ B("ZodOptional", (e, t) => {
	pa.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ys(e, t, n, r), e.unwrap = () => e._zod.def.innerType;
});
function al(e) {
	return new il({
		type: "optional",
		innerType: e
	});
}
var ol = /*@__PURE__*/ B("ZodExactOptional", (e, t) => {
	ma.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ys(e, t, n, r), e.unwrap = () => e._zod.def.innerType;
});
function sl(e) {
	return new ol({
		type: "optional",
		innerType: e
	});
}
var cl = /*@__PURE__*/ B("ZodNullable", (e, t) => {
	ha.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Bs(e, t, n, r), e.unwrap = () => e._zod.def.innerType;
});
function ll(e) {
	return new cl({
		type: "nullable",
		innerType: e
	});
}
var ul = /*@__PURE__*/ B("ZodDefault", (e, t) => {
	ga.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ws(e, t, n, r), e.unwrap = () => e._zod.def.innerType, e.removeDefault = e.unwrap;
});
function dl(e, t) {
	return new ul({
		type: "default",
		innerType: e,
		get defaultValue() {
			return typeof t == "function" ? t() : Jt(t);
		}
	});
}
var fl = /*@__PURE__*/ B("ZodPrefault", (e, t) => {
	va.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Gs(e, t, n, r), e.unwrap = () => e._zod.def.innerType;
});
function pl(e, t) {
	return new fl({
		type: "prefault",
		innerType: e,
		get defaultValue() {
			return typeof t == "function" ? t() : Jt(t);
		}
	});
}
var ml = /*@__PURE__*/ B("ZodNonOptional", (e, t) => {
	ya.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Vs(e, t, n, r), e.unwrap = () => e._zod.def.innerType;
});
function hl(e, t) {
	return new ml({
		type: "nonoptional",
		innerType: e,
		...F(t)
	});
}
var gl = /*@__PURE__*/ B("ZodCatch", (e, t) => {
	Sa.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Ks(e, t, n, r), e.unwrap = () => e._zod.def.innerType, e.removeCatch = e.unwrap;
});
function _l(e, t) {
	return new gl({
		type: "catch",
		innerType: e,
		catchValue: typeof t == "function" ? t : An(t)
	});
}
var vl = /*@__PURE__*/ B("ZodPipe", (e, t) => {
	Ca.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => qs(e, t, n, r), e.in = t.in, e.out = t.out;
});
function yl(e, t) {
	return new vl({
		type: "pipe",
		in: e,
		out: t
	});
}
var bl = /*@__PURE__*/ B("ZodReadonly", (e, t) => {
	Ta.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => Js(e, t, n, r), e.unwrap = () => e._zod.def.innerType;
});
function xl(e) {
	return new bl({
		type: "readonly",
		innerType: e
	});
}
var Sl = /*@__PURE__*/ B("ZodCustom", (e, t) => {
	Da.init(e, t), X.init(e, t), e._zod.processJSONSchema = (t, n, r) => ks(e, t, n, r);
});
function Cl(e, t = {}) {
	return /* @__PURE__ */ Ko(Sl, e, t);
}
function wl(e, t) {
	return /* @__PURE__ */ qo(e, t);
}
//#endregion
//#region studio-src/gate-schemas.ts
var Tl = $({
	code_id: Z(),
	label: Z().nullable(),
	created_at: Z(),
	expires_at: Z().nullable()
}), El = $({
	enabled: Rc(),
	codes: Wc(Tl)
}), Dl = $({ enabled: Rc() }), Ol = $({
	code: Z(),
	code_id: Z(),
	expires_at: Z().nullable()
}), kl = $({ status: tl("revoked") });
//#endregion
//#region studio-src/gate-client.ts
function Al(e, t) {
	return `${e}/gates/${encodeURIComponent(t)}`;
}
function jl(e, t, n, r) {
	return e(Al(t, n), El, { signal: r });
}
function Ml(e, t, n, r) {
	return e(Al(t, n), Dl, {
		method: "PUT",
		body: { enabled: r }
	});
}
function Nl(e, t, n, r) {
	return e(`${Al(t, n)}/codes`, Ol, {
		method: "POST",
		body: {
			label: r.label,
			expires_at: r.expires_at
		}
	});
}
function Pl(e, t, n, r) {
	return e(`${Al(t, n)}/codes/${encodeURIComponent(r)}`, kl, { method: "DELETE" });
}
//#endregion
//#region studio-src/keys.ts
function Fl(e) {
	return [
		"channel-web",
		"entry-gate",
		e
	];
}
function Il(e) {
	return [
		"channel-web",
		"words",
		e
	];
}
//#endregion
//#region studio-src/MintEntryCodeDialog.tsx
var Ll = {
	margin: 0,
	fontSize: "var(--tai-text-sm)",
	color: "var(--tai-color-text-muted)"
}, Rl = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--tai-space-4)"
};
function zl({ base: t, identity: n, onClose: r }) {
	let o = ee(), l = ce(), [u, f] = C(""), [p, m] = C(""), [h, g] = C(null), [_, y] = C(null), x = Tt({
		mutationFn: (e) => Nl(o.request, t, n, e),
		onSuccess: (e) => {
			y(e), l.invalidateQueries({ queryKey: Fl(n) });
		}
	}), te = _ === null ? null : kt(o.baseUrl, t, n, _.code);
	return /* @__PURE__ */ w(a, {
		title: "Mint entry code",
		open: !0,
		dismissable: _ === null,
		onOpenChange: (e) => {
			e || r();
		},
		children: _ !== null && te !== null ? /* @__PURE__ */ T("div", {
			style: Rl,
			children: [
				/* @__PURE__ */ w(i, {
					value: _.code,
					label: "Entry code",
					caption: "This code is shown once — it cannot be shown again. Revoke and mint to get a new one."
				}),
				/* @__PURE__ */ w(i, {
					value: te,
					label: "Chat link",
					caption: "Anyone who opens this link enters the gated route."
				}),
				/* @__PURE__ */ w("p", {
					style: Ll,
					children: _.expires_at === null ? "Never expires." : `Expires ${Et(_.expires_at)}`
				}),
				/* @__PURE__ */ w("div", {
					style: {
						display: "flex",
						justifyContent: "flex-end"
					},
					children: /* @__PURE__ */ w(e, {
						type: "button",
						variant: "primary",
						onClick: r,
						children: "Done"
					})
				})
			]
		}) : /* @__PURE__ */ T("form", {
			"aria-label": "Mint entry code",
			onSubmit: (e) => {
				e.preventDefault(), g(null);
				let t = null;
				if (p !== "") {
					let e = new Date(p);
					if (e.getTime() <= Date.now()) {
						g("Expiry must be in the future.");
						return;
					}
					t = e.toISOString();
				}
				let n = u.trim();
				x.mutate({
					label: n === "" ? null : n,
					expires_at: t
				});
			},
			style: Rl,
			children: [
				/* @__PURE__ */ w(c, {
					label: "Label",
					description: "Optional. A human tag to tell codes apart.",
					children: /* @__PURE__ */ w(v, {
						value: u,
						placeholder: "e.g. newsletter",
						onChange: (e) => {
							f(e.target.value);
						}
					})
				}),
				/* @__PURE__ */ w(c, {
					label: "Expiry",
					description: "Optional. Leave blank for a code that never expires.",
					error: h ?? void 0,
					children: /* @__PURE__ */ w(v, {
						type: "datetime-local",
						value: p,
						onChange: (e) => {
							m(e.target.value);
						}
					})
				}),
				x.isError ? /* @__PURE__ */ w(s, { message: b(x.error) }) : null,
				/* @__PURE__ */ T("div", {
					style: {
						display: "flex",
						justifyContent: "flex-end",
						gap: "var(--tai-space-3)"
					},
					children: [/* @__PURE__ */ w(e, {
						type: "button",
						onClick: r,
						disabled: x.isPending,
						children: "Cancel"
					}), /* @__PURE__ */ T(e, {
						type: "submit",
						variant: "primary",
						disabled: x.isPending,
						children: [x.isPending ? /* @__PURE__ */ w(d, { label: "Minting" }) : null, "Mint code"]
					})]
				})
			]
		})
	});
}
//#endregion
//#region studio-src/query-client.ts
function Bl(e) {
	return e instanceof Error && e.name === "ApiUnauthorizedError";
}
function Vl(e) {
	let t = (t) => {
		Bl(t) && e();
	};
	return new dt({
		defaultOptions: {
			queries: { retry: !1 },
			mutations: { retry: !1 }
		},
		queryCache: new ut({ onError: t }),
		mutationCache: new st({ onError: t })
	});
}
//#endregion
//#region studio-src/RevokeCodeDialog.tsx
function Hl({ isError: t, error: n, isPending: r, onCancel: i, onConfirm: o }) {
	return /* @__PURE__ */ w(a, {
		open: !0,
		title: "Revoke entry code",
		description: "Revoke this code? Its chat link stops working immediately and cannot be restored — a new code means revoke and mint.",
		onOpenChange: (e) => {
			e || i();
		},
		children: /* @__PURE__ */ T("div", {
			style: {
				display: "flex",
				flexDirection: "column",
				gap: "var(--tai-space-4)"
			},
			children: [t ? /* @__PURE__ */ w(s, { message: b(n) }) : null, /* @__PURE__ */ T("div", {
				style: {
					display: "flex",
					justifyContent: "flex-end",
					gap: "var(--tai-space-3)"
				},
				children: [/* @__PURE__ */ w(e, {
					onClick: i,
					disabled: r,
					children: "Cancel"
				}), /* @__PURE__ */ T(e, {
					variant: "danger",
					onClick: o,
					disabled: r,
					children: [r ? /* @__PURE__ */ w(d, { label: "Revoking" }) : null, "Revoke code"]
				})]
			})]
		})
	});
}
//#endregion
//#region studio-src/useEntryGate.ts
function Ul(e, t) {
	let n = ee(), r = ce(), [i, a] = C(!1), [o, s] = C(null), c = wt({
		queryKey: Fl(t),
		queryFn: ({ signal: r }) => jl(n.request, e, t, r)
	}), l = () => {
		r.invalidateQueries({ queryKey: Fl(t) });
	}, u = Tt({
		mutationFn: (r) => Ml(n.request, e, t, r),
		onSuccess: () => {
			a(!1), l();
		}
	});
	return {
		gate: c,
		toggleMutation: u,
		revokeMutation: Tt({
			mutationFn: (r) => Pl(n.request, e, t, r),
			onSuccess: () => {
				s(null), l();
			}
		}),
		onToggle: (e) => {
			u.reset(), e && c.data?.codes.length === 0 ? a(!0) : u.mutate(e);
		},
		confirmEnable: i,
		setConfirmEnable: a,
		pendingRevoke: o,
		setPendingRevoke: s
	};
}
//#endregion
//#region studio-src/EntryGatePanel.tsx
var Wl = {
	display: "flex",
	alignItems: "center",
	justifyContent: "space-between",
	gap: "var(--tai-space-3)",
	margin: "0 0 var(--tai-space-4)"
}, Gl = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--tai-space-4)"
}, Kl = {
	margin: 0,
	fontSize: "var(--tai-text-lg)"
};
function ql() {
	return /* @__PURE__ */ T(t, { children: [/* @__PURE__ */ w("div", {
		style: Wl,
		children: /* @__PURE__ */ w("h2", {
			style: Kl,
			children: "Entry gate"
		})
	}), /* @__PURE__ */ w(o, {
		title: "Entry gate unavailable",
		description: "The web channel's routes are not mounted on this deployment, so its entry gate cannot be read or changed here. Load the web channel to manage entry codes."
	})] });
}
function Jl({ route: e }) {
	let t = oe(), n = ne(), r = re(n);
	r.current = n;
	let [i] = C(() => Vl(() => {
		r.current();
	}));
	return t === null ? /* @__PURE__ */ w(ql, {}) : /* @__PURE__ */ w(le, {
		client: i,
		children: /* @__PURE__ */ w(Yl, {
			base: t,
			identity: e.our_identity
		})
	});
}
function Yl({ base: i, identity: a }) {
	let [o, c] = C(!1), { gate: l, toggleMutation: d, revokeMutation: f, onToggle: p, confirmEnable: m, setConfirmEnable: h, pendingRevoke: g, setPendingRevoke: _ } = Ul(i, a), v;
	return v = l.isPending ? /* @__PURE__ */ w(u, { height: 120 }) : l.isError ? /* @__PURE__ */ w(s, {
		message: b(l.error),
		onRetry: () => void l.refetch()
	}) : /* @__PURE__ */ T("div", {
		style: Gl,
		children: [
			/* @__PURE__ */ w(n, {
				label: "Require an entry code",
				checked: l.data.enabled,
				disabled: d.isPending,
				onCheckedChange: p
			}),
			d.isError && !m ? /* @__PURE__ */ w(s, { message: b(d.error) }) : null,
			/* @__PURE__ */ w(Ot, {
				codes: l.data.codes,
				onRevoke: (e) => {
					f.reset(), _(e);
				}
			})
		]
	}), /* @__PURE__ */ T(t, { children: [
		/* @__PURE__ */ T("div", {
			style: Wl,
			children: [/* @__PURE__ */ w("h2", {
				style: Kl,
				children: "Entry gate"
			}), /* @__PURE__ */ w(e, {
				variant: "primary",
				onClick: () => {
					c(!0);
				},
				children: "Mint entry code"
			})]
		}),
		v,
		o ? /* @__PURE__ */ w(zl, {
			base: i,
			identity: a,
			onClose: () => {
				c(!1);
			}
		}) : null,
		m ? /* @__PURE__ */ w(r, {
			title: "Turn on the entry gate",
			confirmLabel: "Turn on anyway",
			pendingLabel: "Turning on",
			confirmVariant: "primary",
			isPending: d.isPending,
			error: d.isError ? d.error : null,
			onConfirm: () => {
				d.mutate(!0);
			},
			onClose: () => {
				d.reset(), h(!1);
			},
			children: /* @__PURE__ */ w("p", {
				style: { margin: 0 },
				children: "No live codes exist, so nobody can enter this route until you mint one. Turn the gate on anyway?"
			})
		}) : null,
		g === null ? null : /* @__PURE__ */ w(Hl, {
			isError: f.isError,
			error: f.error,
			isPending: f.isPending,
			onCancel: () => {
				_(null);
			},
			onConfirm: () => {
				f.mutate(g);
			}
		})
	] });
}
//#endregion
//#region studio-src/words-schemas.ts
var Xl = $({
	heading: Z().optional(),
	text: Z().optional()
}), Zl = [
	{
		name: "not_a_navigation",
		label: "Not opened as a page",
		description: "The chat URL was loaded by something other than a top-level navigation (an embed or a script)."
	},
	{
		name: "link_invalid",
		label: "Link not valid",
		description: "The chat link's query parameters break a bound (a repeated name, a bad name, too long, too many)."
	},
	{
		name: "entry_refused",
		label: "Entry refused",
		description: "The route is gated and the link carries no live entry code."
	},
	{
		name: "page_unavailable",
		label: "Page unavailable",
		description: "The chat page bundle on the server cannot be served."
	}
], Ql = [
	{
		name: "session_ended",
		label: "Session ended",
		description: "The visitor's session is gone (HTTP 401)."
	},
	{
		name: "wrong_site",
		label: "Wrong site",
		description: "The request came from another site (HTTP 403)."
	},
	{
		name: "not_listening",
		label: "Not listening",
		description: "No route answers this chat (HTTP 404)."
	},
	{
		name: "already_answered",
		label: "Already answered",
		description: "The question was already answered (409)."
	},
	{
		name: "busy",
		label: "Busy",
		description: "The assistant is shedding load (HTTP 429 or 503)."
	},
	{
		name: "not_switched_on",
		label: "Not switched on",
		description: "The deployment runs no chat store (501)."
	},
	{
		name: "too_long",
		label: "Too long",
		description: "The request is over the size cap (HTTP 413). Token: {subject}."
	},
	{
		name: "not_sent",
		label: "Not sent",
		description: "The door refused the request's content (HTTP 400 or 422). Token: {subject}."
	},
	{
		name: "failed",
		label: "Failed",
		description: "Any other failure of the door."
	},
	{
		name: "attachment_gone",
		label: "Attachment gone",
		description: "An attachment can no longer be sent with the message."
	},
	{
		name: "attachment_already_sent",
		label: "Attachment already sent",
		description: "An attachment was already sent with another message."
	},
	{
		name: "entry_refused",
		label: "Entry refused",
		description: "A new conversation was refused by the route's entry gate."
	},
	{
		name: "answer_refused",
		label: "Answer refused",
		description: "An answer was refused and may be given again, when the send set no sentence. Token: {reason}."
	},
	{
		name: "answer_retries_spent",
		label: "Answer retries spent",
		description: "An answer was refused and the question can no longer be answered, when the send set no sentence. Token: {reason}."
	},
	{
		name: "subject_message",
		label: "Subject: message",
		description: "The {subject} of a chat message."
	},
	{
		name: "subject_answer",
		label: "Subject: answer",
		description: "The {subject} of a question's answer."
	},
	{
		name: "subject_form",
		label: "Subject: form",
		description: "The {subject} of a form."
	},
	{
		name: "subject_request",
		label: "Subject: request",
		description: "The {subject} of any other request (a new conversation, the chat stream)."
	}
], $l = $({
	page: Zc($c(Zl.map((e) => e.name)), Xl).optional(),
	transport: Zc($c(Ql.map((e) => e.name)), Z()).optional()
}), eu = $({ status: tl("cleared") });
//#endregion
//#region studio-src/words-client.ts
function tu(e, t) {
	return `${e}/words/${encodeURIComponent(t)}`;
}
function nu(e) {
	return e instanceof Error && e.name === "ApiError" && e.status === 404;
}
async function ru(e, t, n, r) {
	try {
		return await e(tu(t, n), $l, { signal: r });
	} catch (e) {
		if (nu(e)) return null;
		throw e;
	}
}
function iu(e, t, n, r) {
	return e(tu(t, n), $l, {
		method: "PUT",
		body: r
	});
}
function au(e, t, n) {
	return e(tu(t, n), eu, { method: "DELETE" });
}
//#endregion
//#region studio-src/WordsPanel.tsx
var ou = {
	display: "flex",
	alignItems: "center",
	justifyContent: "space-between",
	gap: "var(--tai-space-3)",
	margin: "0 0 var(--tai-space-4)"
}, su = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--tai-space-4)"
}, cu = {
	display: "flex",
	alignItems: "center",
	gap: "var(--tai-space-3)"
}, lu = {
	margin: 0,
	fontSize: "var(--tai-text-lg)"
}, uu = {
	margin: 0,
	fontSize: "var(--tai-text-md)"
}, du = {
	margin: 0,
	color: "var(--tai-color-text-muted)"
}, fu = "No words are set for this route, so every page and sentence draws the built-in English.";
function pu(e) {
	return `page.${e}.heading`;
}
function mu(e) {
	return `page.${e}.text`;
}
function hu(e) {
	return `transport.${e}`;
}
var gu = /* @__PURE__ */ new Set([...Zl.flatMap(({ name: e }) => [pu(e), mu(e)]), ...Ql.map(({ name: e }) => hu(e))]);
function _u(e) {
	let t = {};
	for (let { name: n } of Zl) {
		let r = e?.page?.[n];
		t[pu(n)] = r?.heading ?? "", t[mu(n)] = r?.text ?? "";
	}
	for (let { name: n } of Ql) t[hu(n)] = e?.transport?.[n] ?? "";
	return t;
}
function vu(e) {
	let t = {};
	for (let { name: n } of Zl) {
		let r = e[pu(n)] ?? "", i = e[mu(n)] ?? "";
		(r !== "" || i !== "") && (t[n] = {
			...r === "" ? {} : { heading: r },
			...i === "" ? {} : { text: i }
		});
	}
	let n = {};
	for (let { name: t } of Ql) {
		let r = e[hu(t)] ?? "";
		r !== "" && (n[t] = r);
	}
	return {
		...Object.keys(t).length === 0 ? {} : { page: t },
		...Object.keys(n).length === 0 ? {} : { transport: n }
	};
}
function yu(e) {
	if (!(e instanceof Error) || e.name !== "ApiError" || e.status !== 422) return null;
	let t = /^invalid request body: ([^:]+): (.+)$/s.exec(e.message);
	if (t === null) return null;
	let [, n, r] = t;
	return n === void 0 || r === void 0 || !gu.has(n) ? null : {
		path: n,
		reason: r
	};
}
function bu() {
	return /* @__PURE__ */ T(t, { children: [/* @__PURE__ */ w("div", {
		style: ou,
		children: /* @__PURE__ */ w("h2", {
			style: lu,
			children: "Words"
		})
	}), /* @__PURE__ */ w(o, {
		title: "Words unavailable",
		description: "The web channel's routes are not mounted on this deployment, so a route's words cannot be read or changed here. Load the web channel to manage them."
	})] });
}
function xu({ route: e }) {
	let t = oe(), n = ne(), r = re(n);
	r.current = n;
	let [i] = C(() => Vl(() => {
		r.current();
	}));
	return t === null ? /* @__PURE__ */ w(bu, {}) : /* @__PURE__ */ w(le, {
		client: i,
		children: /* @__PURE__ */ w(Su, {
			base: t,
			identity: e.our_identity
		})
	});
}
function Su({ base: e, identity: n }) {
	let r = ee(), i = wt({
		queryKey: Il(n),
		queryFn: ({ signal: t }) => ru(r.request, e, n, t)
	}), a;
	return a = i.isPending ? /* @__PURE__ */ w(u, { height: 240 }) : i.isError ? /* @__PURE__ */ w(s, {
		message: b(i.error),
		onRetry: () => void i.refetch()
	}) : /* @__PURE__ */ w(Cu, {
		base: e,
		identity: n,
		stored: i.data
	}), /* @__PURE__ */ T(t, { children: [/* @__PURE__ */ w("div", {
		style: ou,
		children: /* @__PURE__ */ w("h2", {
			style: lu,
			children: "Words"
		})
	}), a] });
}
function Cu({ base: t, identity: n, stored: i }) {
	let a = ee(), o = ce(), [l, u] = C(() => _u(i)), [d, f] = C({}), [p, m] = C(!1), [h, g] = C(!1), _ = Tt({
		mutationFn: (e) => iu(a.request, t, n, e),
		onSuccess: (e) => {
			o.setQueryData(Il(n), e), u(_u(e)), m(!0);
		},
		onError: (e) => {
			let t = yu(e);
			t !== null && f({ [t.path]: t.reason });
		}
	}), y = Tt({
		mutationFn: () => au(a.request, t, n),
		onSuccess: () => {
			o.setQueryData(Il(n), null), u(_u(null)), f({}), m(!1), g(!1);
		}
	}), x = (e, t) => {
		u((n) => ({
			...n,
			[e]: t
		})), m(!1), f((t) => {
			if (!(e in t)) return t;
			let { [e]: n, ...r } = t;
			return r;
		});
	}, te = (e, t, n) => /* @__PURE__ */ w(c, {
		label: t,
		...n === void 0 ? {} : { description: n },
		...d[e] === void 0 ? {} : { error: d[e] },
		children: /* @__PURE__ */ w(v, {
			value: l[e] ?? "",
			onChange: (t) => {
				x(e, t.target.value);
			}
		})
	}, e), ne = vu(l), S = ne.page === void 0 && ne.transport === void 0;
	return /* @__PURE__ */ T("div", {
		style: su,
		children: [
			i === null ? /* @__PURE__ */ w("p", {
				style: du,
				children: fu
			}) : null,
			/* @__PURE__ */ w("h3", {
				style: uu,
				children: "Refusal pages"
			}),
			/* @__PURE__ */ w("p", {
				style: du,
				children: "The pages the chat link serves in place of the chat when it refuses a visit."
			}),
			Zl.map(({ name: e, label: t, description: n }) => [te(pu(e), `${t}: heading`, n), te(mu(e), `${t}: text`)]),
			/* @__PURE__ */ w("h3", {
				style: uu,
				children: "Chat sentences"
			}),
			/* @__PURE__ */ w("p", {
				style: du,
				children: "The sentences the chat draws when one of its requests is refused."
			}),
			Ql.map(({ name: e, label: t, description: n }) => te(hu(e), t, n)),
			_.isError && yu(_.error) === null ? /* @__PURE__ */ w(s, { message: b(_.error) }) : null,
			/* @__PURE__ */ T("div", {
				style: cu,
				children: [
					/* @__PURE__ */ w(e, {
						variant: "primary",
						disabled: _.isPending || S,
						onClick: () => {
							f({}), m(!1), _.mutate(ne);
						},
						children: "Save words"
					}),
					i === null ? null : /* @__PURE__ */ w(e, {
						variant: "danger",
						onClick: () => {
							y.reset(), g(!0);
						},
						children: "Clear words"
					}),
					p ? /* @__PURE__ */ w("span", {
						role: "status",
						children: "Words saved."
					}) : null
				]
			}),
			h ? /* @__PURE__ */ w(r, {
				title: "Clear the words",
				confirmLabel: "Clear words",
				pendingLabel: "Clearing",
				confirmVariant: "danger",
				isPending: y.isPending,
				error: y.isError ? y.error : null,
				onConfirm: () => {
					y.mutate();
				},
				onClose: () => {
					y.reset(), g(!1);
				},
				children: /* @__PURE__ */ w("p", {
					style: { margin: 0 },
					children: "Every page and sentence of this route will draw the built-in English again."
				})
			}) : null
		]
	});
}
//#endregion
//#region studio-src/WebRoutePanel.tsx
function wu(e) {
	return `${e}/gates`;
}
function Tu(e) {
	return `${e}/words`;
}
var Eu = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--tai-space-4)"
};
function Du(e) {
	let t = oe(), { state: n } = te();
	if (n.status !== "ready") throw Error("the web route panel rendered without a ready capability projection");
	let { projection: r } = n, i = (e) => x(r) || y(r, [e]), a = t === null || i(wu(t)), o = t === null || i(Tu(t));
	return /* @__PURE__ */ T("div", {
		style: Eu,
		children: [a ? /* @__PURE__ */ w(Jl, { ...e }) : null, o ? /* @__PURE__ */ w(xu, { ...e }) : null]
	});
}
//#endregion
//#region studio-src/index.tsx
function Ou(e) {
	let t = e.routeBase("web");
	ae(t), e.registerRoutePanel({
		channel: "web",
		title: "Web chat",
		component: Du,
		...t === null ? {} : { requiredCapabilities: { routes: [wu(t), Tu(t)] } }
	});
}
//#endregion
export { Ou as register };
