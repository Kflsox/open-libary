// leichtmessen (Nutzertest 2, 03.11.2026): Tempo, Tonart und Grundton ohne gehoer. Für Rechner ohne Jeremias'
// Messwerkzeug: dort kamen Tempo und Tonart nur aus dem Dateinamen, 808 fielen im Projektrahmen ganz heraus und
// „Ähnliche“ fand fast nichts. Nur Apple-Bibliotheken (AVFoundation zum Lesen, Accelerate zum Rechnen), keine
// Installation; einrichten.sh baut es mit.
//
// Aufruf: Pfade zeilenweise auf stdin, je Datei eine JSON-Zeile auf stdout:
//   {"pfad", "dauer", "bpm", "bpm_sicher", "ton", "moll", "ton_sicher", "grundton_midi", "grundton_cent", "tonhaltig"}
// oder {"pfad", "fehler"}. Gelesen werden höchstens die ersten 30 s.
//
// Verfahren (bewusst einfach, gehoer misst genauer):
//   Tempo: Onset-Hüllkurve (spektraler Fluss, 1024/256 bei ~22 kHz), Autokorrelation 60 bis 200 BPM mit einer
//          Vorliebe um 120 BPM, danach auf die Loop-Länge eingerastet (ein Loop ist eine ganze Zahl Takte lang).
//   Tonart: Chroma aus 4096er-Spektren (60 bis 2000 Hz), Korrelation mit den Krumhansl-Kessler-Profilen.
//   Grundton: YIN auf 200 ms ab 50 ms nach dem Anschlag (808 gleiten am Anfang), 25 bis 1000 Hz.
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
    var klang: [Double]? = nil          // 24 Mel-Bänder: Mittel und Streuung des Pegels (dB), erste 10 s
    var fehler: String? = nil
}

enum Fehler: Error { case lesen(String) }

func laden(_ url: URL, maxSek: Double = 30) throws -> ([Float], Double, Double) {
    let f = try AVAudioFile(forReading: url)
    let fmt = f.processingFormat
    let sr = fmt.sampleRate
    let dauer = Double(f.length) / sr
    let n = AVAudioFrameCount(min(Double(f.length), maxSek * sr))
    guard n > 0, let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: n) else { throw Fehler.lesen("leer") }
    try f.read(into: buf, frameCount: n)
    let len = Int(buf.frameLength), kan = Int(fmt.channelCount)
    guard let daten = buf.floatChannelData, len > 0 else { throw Fehler.lesen("kein Float-Format") }
    var mono = [Float](repeating: 0, count: len)
    for c in 0..<kan { vDSP_vadd(mono, 1, daten[c], 1, &mono, 1, vDSP_Length(len)) }
    var k = 1 / Float(kan)
    vDSP_vsmul(mono, 1, &k, &mono, 1, vDSP_Length(len))
    // auf rund 22 kHz: Mittel über 2 (bzw. 4) Werte als grober Tiefpass
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
    /// Beträge der ersten n/2 Bins ab x[start]
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
                z.imagp[0] = 0          // im Bin 0 steht sonst Nyquist
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
    // gleitendes Mittel abziehen, nur Anstiege behalten
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
    // Autokorrelation an einem Tempo (gebrochene Verzögerung linear zwischen zwei Rahmen)
    func akBei(_ bpm: Double) -> Double {
        let l = (60.0 / bpm) / hopS
        let i = Int(l), f = l - Double(i)
        guard i >= 1, i + 1 < ak.count else { return 0 }
        return Double(ak[i]) * (1 - f) + Double(ak[i + 1]) * f
    }
    // Ein Loop ist eine ganze Zahl Takte (1, 2, 4, 8, 16, 32 Takte 4/4): dann kommen nur diese Tempi in Frage, und die
    // Autokorrelation wählt zwischen ihnen (Prüfung 03.11.: frei geschätzt lagen 26 von 300 bei 4/3 des richtigen Tempos)
    if dauer >= 1.5 {
        var kand: [Double] = []
        for takte in [1.0, 2, 4, 8, 16, 32] {
            let k = 240.0 * takte / dauer
            if k >= 60 && k <= 200 { kand.append(k) }
        }
        if !kand.isEmpty {
            var bestK = kand[0], bestW = -1.0
            for k in kand {
                let vorliebe = exp(-0.5 * pow(log2(k / 110.0) / 0.8, 2))
                let w = (akBei(k) + 0.5 * akBei(k * 2)) * vorliebe
                if w > bestW { bestW = w; bestK = k }
            }
            var bpm = bestK
            if abs(bpm - bpm.rounded()) < 0.15 { bpm = bpm.rounded() }
            return (bpm, min(9.9, akBei(bestK) / akMittel))
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
    // Feinlage per Parabel
    let a = Double(ak[besterLag - 1]), b = Double(ak[besterLag]), c = Double(ak[besterLag + 1])
    let versatz = (a - 2 * b + c) != 0 ? 0.5 * (a - c) / (a - 2 * b + c) : 0
    var bpm = 60.0 / ((Double(besterLag) + max(-0.5, min(0.5, versatz))) * hopS)
    let sicher = Double(ak[besterLag]) / akMittel
    // Loop-Länge: ein Loop ist eine ganze Zahl Takte; dann ist das Tempo genau 240 * Takte / Dauer
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

// ---------------------------------------------------------------- Tonart
let DUR: [Double] = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
let MOLL: [Double] = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

func korrelation(_ a: [Double], _ b: [Double]) -> Double {
    let ma = a.reduce(0, +) / 12, mb = b.reduce(0, +) / 12
    var z = 0.0, na = 0.0, nb = 0.0
    for i in 0..<12 { z += (a[i] - ma) * (b[i] - mb); na += pow(a[i] - ma, 2); nb += pow(b[i] - mb, 2) }
    return z / (sqrt(na * nb) + 1e-12)
}

func tonart(_ x: [Float], sr: Double) -> (Int, Bool, Double)? {
    // 8192 bei ~22 kHz: 2,7 Hz je Bin; unter 100 Hz liegen Halbtöne enger als das, darum erst ab 100 Hz. Nur Bins bis
    // 25 Cent neben einem Halbton zählen (Prüfung 03.11.: mit 4096 und allen Bins lag jede fünfte Tonart eine Quinte daneben)
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
    // Tonhöhen statt Bins: je Halbton C2 bis C7 die Summe seiner ersten vier Obertöne (0,8 je Stufe leiser). So zählt ein
    // Grundton mehr als die Quinte, die sein dritter Oberton ist (Prüfung 03.11.: 21 % der Tonarten eine Quinte zu hoch)
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
    // Sicherheit: Abstand zur zweitbesten, die nicht die Parallele ist (C und Am teilen alle Töne)
    let erster = werte[0]
    let parallele = erster.2 ? (erster.1 + 3) % 12 : (erster.1 + 9) % 12
    let zweiter = werte.dropFirst().first { !($0.1 == parallele && $0.2 != erster.2) } ?? werte[1]
    return (erster.1, erster.2, max(0, erster.0) * 10 * (erster.0 - zweiter.0))
}

// ---------------------------------------------------------------- Grundton
func grundton(_ x: [Float], sr: Double) -> (Double, Double)? {
    // Anschlag: lautester 512er-Rahmen in der ersten Sekunde
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
    // mehrere Fenster ab 100 ms nach dem Anschlag, der Median zählt (Prüfung 03.11.: ab 50 ms lag jedes siebte 808 einen
    // Halbton zu tief, es gleitet noch); kurze Sounds: so viele Fenster, wie passen
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
    // kumulativ normiert (YIN)
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
        if minWert > 0.4 { return nil }      // kein Ton erkennbar (Snare, Rauschen)
    }
    let a = Double(dn[wahl - 1]), b = Double(dn[wahl]), c = Double(dn[min(wahl + 1, tauMax)])
    let versatz = (a - 2 * b + c) != 0 ? 0.5 * (a - c) / (a - 2 * b + c) : 0
    let f0 = sr / (Double(wahl) + max(-0.5, min(0.5, versatz)))
    return (69 + 12 * log2(f0 / 440), 1 - Double(dn[wahl]))
}

// ---------------------------------------------------------------- Klang (für „Ähnliche“ ohne gehoer)
// Mittel und Streuung des Pegels in 24 Mel-Bändern (60 Hz bis 8 kHz) über die ersten 10 s: grob, aber es trennt Kick
// von Snare, dunkle von hellen Loops, Vinyl von sauber. Der Server standardisiert und vergleicht per Kosinus.
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

// ---------------------------------------------------------------- Ablauf
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
        // nur endliche Zahlen (eine Float-WAV mit NaN ließ JSONEncoder scheitern, dann fehlte die Zeile ganz)
        func endlich(_ x: Double?) -> Double? { x.flatMap { $0.isFinite ? $0 : nil } }
        e.dauer = endlich(e.dauer); e.bpm = endlich(e.bpm); e.bpm_sicher = endlich(e.bpm_sicher)
        e.ton_sicher = endlich(e.ton_sicher); e.tonhaltig = endlich(e.tonhaltig)
        if let k = e.klang, k.contains(where: { !$0.isFinite }) { e.klang = nil }
        if let d = try? kodierer.encode(e), let s = String(data: d, encoding: .utf8) {
            print(s)
        } else if let d = try? kodierer.encode(Ergebnis(pfad: pfad, fehler: "nicht kodierbar")),
                  let s = String(data: d, encoding: .utf8) {
            print(s)
        }
        fflush(stdout)
    }
}
