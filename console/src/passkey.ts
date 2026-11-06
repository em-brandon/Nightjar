// Passkeys (WebAuthn) in the browser. The hub speaks JSON with every binary value base64url-encoded; the browser's
// API wants ArrayBuffers. These functions convert in both directions and are the only place that does, so the
// conversions can be tested without a browser (passkey.test.mjs).

export function bytesToB64url(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let binary = "";
  for (const b of view) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function b64urlToBytes(text: string): Uint8Array<ArrayBuffer> {
  const padded = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
  const binary = atob(padded);
  const out = new Uint8Array(new ArrayBuffer(binary.length));
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

interface CredentialRef {
  id: string;
  type: string;
  transports?: string[];
}

// the options object as the hub sends it (py_webauthn's JSON form)
export interface WireOptions {
  challenge: string;
  rp?: { id?: string; name: string };
  rpId?: string;
  user?: { id: string; name: string; displayName: string };
  timeout?: number;
  userVerification?: string;
  pubKeyCredParams?: { type: string; alg: number }[];
  authenticatorSelection?: Record<string, string>;
  excludeCredentials?: CredentialRef[];
  allowCredentials?: CredentialRef[];
  attestation?: string;
}

const descriptor = (c: CredentialRef) => ({ ...c, id: b64urlToBytes(c.id) }) as PublicKeyCredentialDescriptor;

export function creationOptions(o: WireOptions): PublicKeyCredentialCreationOptions {
  if (!o.user || !o.rp || !o.pubKeyCredParams) throw new Error("the hub sent incomplete passkey options");
  return {
    ...o,
    rp: o.rp,
    user: { ...o.user, id: b64urlToBytes(o.user.id) },
    pubKeyCredParams: o.pubKeyCredParams as PublicKeyCredentialParameters[],
    challenge: b64urlToBytes(o.challenge),
    excludeCredentials: (o.excludeCredentials ?? []).map(descriptor),
  } as PublicKeyCredentialCreationOptions;
}

export function requestOptions(o: WireOptions): PublicKeyCredentialRequestOptions {
  return {
    ...o,
    challenge: b64urlToBytes(o.challenge),
    allowCredentials: (o.allowCredentials ?? []).map(descriptor),
  } as PublicKeyCredentialRequestOptions;
}

export const supported = (): boolean => typeof window !== "undefined" && "PublicKeyCredential" in window && Boolean(navigator.credentials);

export async function createPasskey(o: WireOptions): Promise<Record<string, unknown>> {
  const cred = (await navigator.credentials.create({ publicKey: creationOptions(o) })) as PublicKeyCredential | null;
  if (!cred) throw new Error("No passkey was created.");
  const r = cred.response as AuthenticatorAttestationResponse;
  return {
    id: cred.id,
    rawId: bytesToB64url(cred.rawId),
    type: cred.type,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: bytesToB64url(r.clientDataJSON),
      attestationObject: bytesToB64url(r.attestationObject),
      transports: r.getTransports?.() ?? [],
    },
  };
}

export async function getPasskey(o: WireOptions): Promise<Record<string, unknown>> {
  const cred = (await navigator.credentials.get({ publicKey: requestOptions(o) })) as PublicKeyCredential | null;
  if (!cred) throw new Error("No passkey was chosen.");
  const r = cred.response as AuthenticatorAssertionResponse;
  return {
    id: cred.id,
    rawId: bytesToB64url(cred.rawId),
    type: cred.type,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: bytesToB64url(r.clientDataJSON),
      authenticatorData: bytesToB64url(r.authenticatorData),
      signature: bytesToB64url(r.signature),
      userHandle: r.userHandle ? bytesToB64url(r.userHandle) : null,
    },
  };
}

// The browser reports cancelling, a timeout and "no such passkey" the same way (NotAllowedError). Say what is useful.
export function passkeyProblem(err: unknown): string {
  if (err instanceof DOMException) {
    if (err.name === "NotAllowedError") return "The passkey prompt was cancelled or timed out.";
    if (err.name === "InvalidStateError") return "That device already has a passkey for this account.";
    if (err.name === "SecurityError") return "This page's address does not match the hub's passkey settings (WEBAUTHN_RP_ID).";
  }
  return err instanceof Error ? err.message : String(err);
}
