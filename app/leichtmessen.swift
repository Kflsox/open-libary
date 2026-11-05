// leichtmessen: tempo, key and root note without gehoer. For computers without the author's analysis tool: there
// tempo and key came only from the file name, 808s dropped out of the project frame entirely and "Similar" found
// almost nothing. Only Apple libraries (AVFoundation for reading, Accelerate for the math), nothing to install;
// einrichten.sh builds it along with the app.
//
// Usage: paths line by line on stdin, one JSON line per file on stdout:
//   {"pfad", "dauer", "bpm", "bpm_sicher", "ton", "moll", "ton_sicher", "grundton_midi", "grundton_cent", "tonhaltig"}
// or {"pfad", "fehler"}. At most the first 30 s are read.
//
// Method (deliberately simple, gehoer measures more precisely):
//   Tempo: for a loop the tempos that make it a whole number of bars, the one nearest to 110 BPM; otherwise onset
//          envelope (spectral flux, 1024/256 at ~22 kHz), autocorrelation 60 to 200 BPM with a preference around
//          120 BPM, then snapped to the loop length.
//   Key: chroma from 8192-point spectra (pitches C2 to C7, each the sum of its first four harmonics),
//        correlation with the Krumhansl-Kessler profiles.
//   Root note: YIN on 200 ms from 50 ms after the attack (808s glide at the start), 25 to 1000 Hz.
import Foundation
import AVFoundation
import Accelerate

struct Ergebnis: Encodable {
    var pfad: String
    var dauer: Double? = nil
    var bpm: Double? = nil
    var bpm_sicher: Double? = nil
    var ton: Int? = nil
    var moll: Bool? = nil
    var ton_sicher: Double? = nil
    var grundton_midi: Int? = nil
    var grundton_cent: Int? = nil
    var tonhaltig: Double? = nil
    var klang: [Double]? = nil          // 24 mel bands: mean and spread of the level (dB), first 10 s
    var fehler: String? = nil
}

enum Fehler: Error { case lesen(String) }

func laden(_ url: URL, maxSek: Double = 30) throws -> ([Float], Double, Double) {
    let f = try AVAudioFile(forReading: url)
    let fmt = f.processingFormat
    let sr = fmt.sampleRate
    let dauer = Double(f.length) / sr
    let n = AVAudioFrameCount(min(Double(f.length), maxSek * sr))
    guard n > 0, let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: n) else { throw Fehler.lesen("empty") }
    try f.read(into: buf, frameCount: n)
    let len = Int(buf.frameLength), kan = Int(fmt.channelCount)
    guard let daten = buf.floatChannelData, len > 0 else { throw Fehler.lesen("not a float format") }
    var mono = [Float](repeating: 0, count: len)
    for c in 0..<kan { vDSP_vadd(mono, 1, daten[c], 1, &mono, 1, vDSP_Length(len)) }
    var k = 1 / Float(kan)
    vDSP_vsmul(mono, 1, &k, &mono, 1, vDSP_Length(len))
    // down to about 22 kHz: mean over 2 (or 4) values as a rough low-pass
    let faktor = sr >= 88_000 ? 4 : sr >= 44_000 ? 2 : 1
    if faktor == 1 { return (mono, sr, dauer) }
    let m = len / faktor
    var aus = [Float](repeating: 0, count: m)
    for i in 0..<m {
        var s: Float = 0
        for j in 0..<faktor { s += mono[i * faktor + j] }
        aus[i] = s / Float(faktor)
    }
    return (aus, sr / Double(faktor), dauer)
}

final class Spektrum {
    let n: Int, log2n: vDSP_Length, setup: FFTSetup
    var fenster: [Float]
    var re: [Float], im: [Float], w: [Float]
    init(_ n: Int) {
        self.n = n
        log2n = vDSP_Length(log2(Double(n)))
        setup = vDSP_create_fftsetup(log2n, FFTRadix(kFFTRadix2))!
        fenster = [Float](repeating: 0, count: n)
        vDSP_hann_window(&fenster, vDSP_Length(n), Int32(vDSP_HANN_NORM))
        re = [Float](repeating: 0, count: n / 2); im = re; w = fenster
    }
    deinit { vDSP_destroy_fftsetup(setup) }
    /// Magnitudes of the first n/2 bins from x[start]
    func betrag(_ x: [Float], _ start: Int) -> [Float] {
        x.withUnsafeBufferPointer { xp in vDSP_vmul(xp.baseAddress! + start, 1, fenster, 1, &w, 1, vDSP_Length(n)) }
        var mag = [Float](repeating: 0, count: n / 2)
        re.withUnsafeMutableBufferPointer { rp in
            im.withUnsafeMutableBufferPointer { ip in
                var z = DSPSplitComplex(realp: rp.baseAddress!, imagp: ip.baseAddress!)
                w.withUnsafeBufferPointer { wp in
                    wp.baseAddress!.withMemoryRebound(to: DSPComplex.self, capacity: n / 2) {
                        vDSP_ctoz($0, 2, &z, 1, vDSP_Length(n / 2))
                    }
                }
                vDSP_fft_zrip(setup, &z, 1, log2n, FFTDirection(FFT_FORWARD))
                z.imagp[0] = 0          // bin 0 holds Nyquist otherwise
                vDSP_zvabs(&z, 1, &mag, 1, vDSP_Length(n / 2))
            }
        }
        return mag
    }
}

// ---------------------------------------------------------------- Tempo
func tempo(_ x: [Float], sr: Double, dauer: Double) -> (Double, Double)? {
    let n = 1024, hop = 256
    guard x.count > n * 8 else { return nil }
    let fft = Spektrum(n)
    let rahmen = (x.count - n) / hop + 1
    var vorher = [Float](repeating: 0, count: n / 2)
    var huelle = [Float](repeating: 0, count: rahmen)
    for i in 0..<rahmen {
        var mag = fft.betrag(x, i * hop)
        var eins: Float = 1, faktor: Float = 1000
        vDSP_vsmsa(mag, 1, &faktor, &eins, &mag, 1, vDSP_Length(n / 2))     // 1 + 1000 * |X|
        var cnt = Int32(n / 2)
        vvlogf(&mag, mag, &cnt)
        var fluss: Float = 0
        for b in 0..<(n / 2) { let d = mag[b] - vorher[b]; if d > 0 { fluss += d } }
        huelle[i] = fluss
        vorher = mag
    }
    // subtract the moving average, keep only rises
    let breite = 16
    var mittel = [Float](repeating: 0, count: rahmen)
    var summe: Float = 0
    for i in 0..<rahmen {
        summe += huelle[i]
        if i >= breite { summe -= huelle[i - breite] }
        mittel[i] = summe / Float(min(i + 1, breite))
    }
    for i in 0..<rahmen { huelle[i] = max(0, huelle[i] - mittel[i]) }
    let hopS = Double(hop) / sr
    let minLag = max(1, Int((60.0 / 200.0) / hopS)), maxLag = Int((60.0 / 60.0) / hopS) + 1
    guard rahmen > maxLag * 2 else { return nil }
    var ak = [Float](repeating: 0, count: maxLag + 2)
    huelle.withUnsafeBufferPointer { h in
        for lag in minLag...(maxLag + 1) {
            var s: Float = 0
            vDSP_dotpr(h.baseAddress!, 1, h.baseAddress! + lag, 1, &s, vDSP_Length(rahmen - lag))
            ak[lag] = s / Float(rahmen - lag)
        }
    }
    var akSumme: Float = 0
    for lag in minLag...maxLag { akSumme += ak[lag] }
    let akMittel = Double(akSumme) / Double(maxLag - minLag + 1) + 1e-9
    // Autocorrelation at a tempo (fractional lag interpolated linearly between two frames)
    func akBei(_ bpm: Double) -> Double {
        let l = (60.0 / bpm) / hopS
        let i = Int(l), f = l - Double(i)
        guard i >= 1, i + 1 < ak.count else { return 0 }
        return Double(ak[i]) * (1 - f) + Double(ak[i + 1]) * f
    }
    // A loop is a whole number of bars (1, 2, 4, 8, 16, 32 bars of 4/4): then only these tempos qualify (estimated
    // freely, 26 of 300 landed at 4/3 of the right tempo). They lie an octave apart, and which octave a loop is
    // labeled with is a convention the audio does not settle: weighting them by the autocorrelation put a third of
    // the loops at half their tempo. Loops are mostly labeled between 78 and 155 BPM, so the candidate nearest to
    // 110 BPM wins (check 05.11.2026 in leicht.py). The confidence is the strongest periodicity among the
    // candidates: it says whether the loop follows the bar grid at all, not which octave.
    if dauer >= 1.5 {
        var kand: [Double] = []
        for takte in [1.0, 2, 4, 8, 16, 32] {
            let k = 240.0 * takte / dauer
            if k >= 60 && k <= 200 { kand.append(k) }
        }
        if !kand.isEmpty {
            let bestK = kand.min(by: { abs(log2($0 / 110.0)) < abs(log2($1 / 110.0)) })!
            var bpm = bestK
            if abs(bpm - bpm.rounded()) < 0.15 { bpm = bpm.rounded() }
            return (bpm, min(9.9, kand.map(akBei).max()! / akMittel))
        }
    }
    var bester = 0.0, besterLag = minLag
    for lag in minLag...maxLag {
        let bpm = 60.0 / (Double(lag) * hopS)
        let vorliebe = exp(-0.5 * pow(log2(bpm / 120.0) / 0.9, 2))
        let wert = Double(ak[lag]) * vorliebe
        if wert > bester { bester = wert; besterLag = lag }
    }
    guard bester > 0 else { return nil }
    // Fine position by parabola
    let a = Double(ak[besterLag - 1]), b = Double(ak[besterLag]), c = Double(ak[besterLag + 1])
    let versatz = (a - 2 * b + c) != 0 ? 0.5 * (a - c) / (a - 2 * b + c) : 0
    var bpm = 60.0 / ((Double(besterLag) + max(-0.5, min(0.5, versatz))) * hopS)
    let sicher = Double(ak[besterLag]) / akMittel
    // Loop length: a loop is a whole number of bars; then the tempo is exactly 240 * bars / duration
    var eingerastet = false
    if dauer >= 1.5 {
        var naechst = Double.infinity, ziel = bpm
        for takte in [1.0, 2, 3, 4, 6, 8, 12, 16, 24, 32] {
            let k = 240.0 * takte / dauer
            for kk in [k, k * 2, k / 2] where kk >= 55 && kk <= 210 {
                let d = abs(bpm / kk - 1)
                if d < 0.04 && d < naechst { naechst = d; ziel = kk }
            }
        }
        if naechst.isFinite { bpm = ziel; eingerastet = true }
    }
    if abs(bpm - bpm.rounded()) < 0.15 { bpm = bpm.rounded() }
    return (bpm, min(9.9, sicher) * (eingerastet ? 1.0 : 0.7))
}

// ---------------------------------------------------------------- Key
let DUR: [Double] = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
let MOLL: [Double] = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

func korrelation(_ a: [Double], _ b: [Double]) -> Double {
    let ma = a.reduce(0, +) / 12, mb = b.reduce(0, +) / 12
    var z = 0.0, na = 0.0, nb = 0.0
    for i in 0..<12 { z += (a[i] - ma) * (b[i] - mb); na += pow(a[i] - ma, 2); nb += pow(b[i] - mb, 2) }
    return z / (sqrt(na * nb) + 1e-12)
}

func tonart(_ x: [Float], sr: Double) -> (Int, Bool, Double)? {
    // 8192 at ~22 kHz: 2.7 Hz per bin; below 100 Hz semitones lie closer than that, so only from 100 Hz on. Only bins
    // within 25 cents of a semitone count (check: with 4096 and all bins, every fifth key was off by a fifth)
    let n = 8192, hop = 4096
    guard x.count > n else { return nil }
    let fft = Spektrum(n)
    var klasse = [Int](repeating: -1, count: n / 2)
    for b in 1..<(n / 2) {
        let f = Double(b) * sr / Double(n)
        if f < 100 || f > 2500 { continue }
        let halbton = 12 * log2(f / 440)
        if abs(halbton - halbton.rounded()) > 0.25 { continue }
        klasse[b] = ((Int(halbton.rounded()) + 69) % 12 + 12) % 12
    }
    // Pitches instead of bins: per semitone C2 to C7 the sum of its first four harmonics (each 0.8 quieter). So a
    // root note counts more than the fifth, which is its third harmonic (check: 21 % of the keys a fifth too high)
    _ = klasse
    var chroma = [Double](repeating: 0, count: 12)
    var bins: [[Int]] = []
    for p in 36...96 {
        let f = 440 * pow(2, Double(p - 69) / 12)
        bins.append((1...4).map { Int((Double($0) * f * Double(n) / sr).rounded()) })
    }
    var i = 0
    while i + n <= x.count {
        let mag = fft.betrag(x, i)
        for (k, bs) in bins.enumerated() {
            var s = 0.0, gewicht = 1.0
            for b in bs {
                if b + 1 < n / 2 && b > 0 { s += gewicht * Double(max(mag[b - 1], mag[b], mag[b + 1])) }
                gewicht *= 0.8
            }
            chroma[(k + 36) % 12] += s
        }
        i += hop
    }
    guard chroma.reduce(0, +) > 0 else { return nil }
    var werte: [(Double, Int, Bool)] = []
    for t in 0..<12 {
        let gedreht = (0..<12).map { chroma[($0 + t) % 12] }
        werte.append((korrelation(gedreht, DUR), t, false))
        werte.append((korrelation(gedreht, MOLL), t, true))
    }
    werte.sort { $0.0 > $1.0 }
    // Confidence: distance to the second best that is not the relative key (C and Am share all notes)
    let erster = werte[0]
    let parallele = erster.2 ? (erster.1 + 3) % 12 : (erster.1 + 9) % 12
    let zweiter = werte.dropFirst().first { !($0.1 == parallele && $0.2 != erster.2) } ?? werte[1]
    return (erster.1, erster.2, max(0, erster.0) * 10 * (erster.0 - zweiter.0))
}

// ---------------------------------------------------------------- Root note
func grundton(_ x: [Float], sr: Double) -> (Double, Double)? {
    // Attack: loudest 512-sample frame in the first second
    let r = 512
    var spitze = 0, spitzeWert: Float = 0
    var j = 0
    while j + r <= min(x.count, Int(sr)) {
        var e: Float = 0
        x.withUnsafeBufferPointer { vDSP_svesq($0.baseAddress! + j, 1, &e, vDSP_Length(r)) }
        if e > spitzeWert { spitzeWert = e; spitze = j }
        j += r / 2
    }
    let tauMin = Int(sr / 1000), tauMax = Int(sr / 25)
    let breite = Int(0.12 * sr)
    // several windows from 100 ms after the attack, the median counts (check: from 50 ms every seventh 808 was a
    // semitone too low, it is still gliding); short sounds: as many windows as fit
    var tonhoehen: [(Double, Double)] = []
    for versatzMs in stride(from: 100, through: 600, by: 100) {
        let start = spitze + Int(Double(versatzMs) / 1000 * sr)
        guard start + breite + tauMax <= x.count else { break }
        if let t = yin(x, start: start, breite: breite, tauMin: tauMin, tauMax: tauMax, sr: sr) { tonhoehen.append(t) }
    }
    if tonhoehen.isEmpty {
        let start = spitze + Int(0.04 * sr)
        guard start + breite + tauMax <= x.count,
              let t = yin(x, start: start, breite: breite, tauMin: tauMin, tauMax: tauMax, sr: sr) else { return nil }
        tonhoehen.append(t)
    }
    let sortiert = tonhoehen.sorted { $0.0 < $1.0 }
    return sortiert[sortiert.count / 2]
}

func yin(_ x: [Float], start: Int, breite: Int, tauMin: Int, tauMax: Int, sr: Double) -> (Double, Double)? {
    var d = [Float](repeating: 0, count: tauMax + 1)
    var diff = [Float](repeating: 0, count: breite)
    x.withUnsafeBufferPointer { xp in
        let p = xp.baseAddress! + start
        for tau in 1...tauMax {
            vDSP_vsub(p + tau, 1, p, 1, &diff, 1, vDSP_Length(breite))
            var s: Float = 0
            vDSP_svesq(diff, 1, &s, vDSP_Length(breite))
            d[tau] = s
        }
    }
    // cumulative mean normalized difference (YIN)
    var dn = [Float](repeating: 1, count: tauMax + 1)
    var lauf: Float = 0
    for tau in 1...tauMax {
        lauf += d[tau]
        dn[tau] = lauf > 0 ? d[tau] * Float(tau) / lauf : 1
    }
    var wahl = -1
    var tau = tauMin
    while tau < tauMax {
        if dn[tau] < 0.15 {
            while tau + 1 < tauMax && dn[tau + 1] < dn[tau] { tau += 1 }
            wahl = tau; break
        }
        tau += 1
    }
    if wahl < 0 {
        var minWert: Float = 9
        for t in tauMin..<tauMax where dn[t] < minWert { minWert = dn[t]; wahl = t }
        if minWert > 0.4 { return nil }      // no pitch detectable (snare, noise)
    }
    let a = Double(dn[wahl - 1]), b = Double(dn[wahl]), c = Double(dn[min(wahl + 1, tauMax)])
    let versatz = (a - 2 * b + c) != 0 ? 0.5 * (a - c) / (a - 2 * b + c) : 0
    let f0 = sr / (Double(wahl) + max(-0.5, min(0.5, versatz)))
    return (69 + 12 * log2(f0 / 440), 1 - Double(dn[wahl]))
}

// ---------------------------------------------------------------- Timbre (for "Similar" without gehoer)
// Mean and spread of the level in 24 mel bands (60 Hz to 8 kHz) over the first 10 s: rough, but it separates kick
// from snare, dark from bright loops, vinyl from clean. The server standardizes and compares by cosine.
func klangVektor(_ x: [Float], sr: Double) -> [Double]? {
    let n = 1024, hop = 512, baender = 24
    let ende = min(x.count, Int(10 * sr))
    guard ende > n * 2 else { return nil }
    let fft = Spektrum(n)
    func mel(_ f: Double) -> Double { 2595 * log10(1 + f / 700) }
    func hz(_ m: Double) -> Double { 700 * (pow(10, m / 2595) - 1) }
    let lo = mel(60), hi = mel(min(8000, sr / 2 - 100))
    let punkte = (0...(baender + 1)).map { hz(lo + (hi - lo) * Double($0) / Double(baender + 1)) }
    let binHz = sr / Double(n)
    var filter: [[(Int, Float)]] = []
    for b in 0..<baender {
        var f: [(Int, Float)] = []
        let (l, m, r) = (punkte[b], punkte[b + 1], punkte[b + 2])
        for k in max(1, Int(l / binHz))...min(n / 2 - 1, Int(r / binHz) + 1) {
            let fk = Double(k) * binHz
            let w = fk < m ? (fk - l) / (m - l) : (r - fk) / (r - m)
            if w > 0 { f.append((k, Float(w))) }
        }
        filter.append(f)
    }
    var summe = [Double](repeating: 0, count: baender), quadrat = summe
    var rahmen = 0, i = 0
    while i + n <= ende {
        let mag = fft.betrag(x, i)
        for b in 0..<baender {
            var e: Float = 0
            for (k, w) in filter[b] { e += w * mag[k] * mag[k] }
            let db = 10 * log10(Double(e) + 1e-10)
            summe[b] += db; quadrat[b] += db * db
        }
        rahmen += 1; i += hop
    }
    guard rahmen > 1 else { return nil }
    var v: [Double] = []
    for b in 0..<baender { v.append(summe[b] / Double(rahmen)) }
    for b in 0..<baender { v.append(max(0, quadrat[b] / Double(rahmen) - pow(summe[b] / Double(rahmen), 2)).squareRoot()) }
    return v.map { ($0 * 10).rounded() / 10 }
}

// ---------------------------------------------------------------- Main loop
let kodierer = JSONEncoder()
while let zeile = readLine() {
    let pfad = zeile.trimmingCharacters(in: .whitespacesAndNewlines)
    if pfad.isEmpty { continue }
    autoreleasepool {
        var e = Ergebnis(pfad: pfad)
        do {
            let (x, sr, dauer) = try laden(URL(fileURLWithPath: pfad))
            e.dauer = (dauer * 1000).rounded() / 1000
            if let (b, s) = tempo(x, sr: sr, dauer: dauer), b.isFinite, s.isFinite { e.bpm = (b * 100).rounded() / 100; e.bpm_sicher = (s * 100).rounded() / 100 }
            if let (t, m, s) = tonart(x, sr: sr) { e.ton = t; e.moll = m; e.ton_sicher = (s * 100).rounded() / 100 }
            e.klang = klangVektor(x, sr: sr)
            if let (midi, g) = grundton(x, sr: sr), midi.isFinite, g.isFinite, midi > 0, midi < 140 {
                let rund = midi.rounded()
                e.grundton_midi = Int(rund); e.grundton_cent = Int(((midi - rund) * 100).rounded())
                e.tonhaltig = (g * 100).rounded() / 100
            }
        } catch {
            e.fehler = "\(error)".prefix(200).description
        }
        // only finite numbers (a float WAV with NaN made JSONEncoder fail, then the line was missing entirely)
        func endlich(_ x: Double?) -> Double? { x.flatMap { $0.isFinite ? $0 : nil } }
        e.dauer = endlich(e.dauer); e.bpm = endlich(e.bpm); e.bpm_sicher = endlich(e.bpm_sicher)
        e.ton_sicher = endlich(e.ton_sicher); e.tonhaltig = endlich(e.tonhaltig)
        if let k = e.klang, k.contains(where: { !$0.isFinite }) { e.klang = nil }
        if let d = try? kodierer.encode(e), let s = String(data: d, encoding: .utf8) {
            print(s)
        } else if let d = try? kodierer.encode(Ergebnis(pfad: pfad, fehler: "not encodable")),
                  let s = String(data: d, encoding: .utf8) {
            print(s)
        }
        fflush(stdout)
    }
}
