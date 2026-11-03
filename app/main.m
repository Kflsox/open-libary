// Open Libary.app: Fenster um die Open Libary-Seite (http://127.0.0.1:8765/) mit echtem Drag & Drop in die DAW.
// Ein Browser kann keine Datei vom Rechner in eine DAW ziehen lassen; diese App schon: die Seite meldet beim Ziehen den
// Pfad, die App startet damit eine native Ziehsitzung mit der Originaldatei (keine Kopie).
//
// Objective-C seit 03.11.2026 („max kompi“): das Swift der aktuellen Command Line Tools baut für Intel nur noch ab
// macOS 13; Objective-C läuft als Universal-App auf Apple-Chip ab macOS 11 und Intel ab 10.15. Verhalten wie die
// frühere main.swift (app/alt/). Bauen: bash app/bauen.sh (eigene Einrichtung) oder bash app/app_paket.sh (fertige App).
#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

static NSString *const ADRESSE = @"http://127.0.0.1:8765/";

static NSString *starterPfad(void) { return [NSHomeDirectory() stringByAppendingPathComponent:@".local/bin/openlibary"]; }

// Fertige App (app/app_paket.sh): Python und Code liegen im Programmpaket, je Architektur ein eigenes Python; die
// Nutzerdaten unter ~/Library/Application Support/Open Libary. Fehlen sie (Jeremias' Einrichtung), startet der Starter.
static NSString *paketPython(void) {
#if defined(__arm64__)
    NSString *arch = @"arm64";
#else
    NSString *arch = @"x86_64";
#endif
    NSString *r = [[NSBundle mainBundle] resourcePath];
    NSString *jeArch = [r stringByAppendingPathComponent:[NSString stringWithFormat:@"python-%@/bin/python3", arch]];
    if ([[NSFileManager defaultManager] isExecutableFileAtPath:jeArch]) return jeArch;
    return [r stringByAppendingPathComponent:@"python/bin/python3"];
}
static NSString *paketCode(void) {
    return [[[NSBundle mainBundle] resourcePath] stringByAppendingPathComponent:@"openlibary/openlibary.py"];
}
static NSString *datenOrdner(void) {
    return [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/Open Libary"];
}

@interface Fenster : NSObject <NSApplicationDelegate, WKScriptMessageHandler, WKUIDelegate, WKNavigationDelegate,
                               NSDraggingSource, NSWindowDelegate>
@property (strong) NSWindow *fenster;
@property (strong) WKWebView *web;
@property (strong) NSEvent *letzteMaus;
@property (assign) NSInteger versuche;
@end

@implementation Fenster

- (void)applicationDidFinishLaunching:(NSNotification *)n {
    WKWebViewConfiguration *konf = [[WKWebViewConfiguration alloc] init];
    konf.mediaTypesRequiringUserActionForPlayback = WKAudiovisualMediaTypeNone;
    for (NSString *name in @[@"ziehen", @"kopieren", @"finder", @"ordner"])
        [konf.userContentController addScriptMessageHandler:self name:name];
    self.web = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:konf];
    self.web.UIDelegate = self;
    self.web.navigationDelegate = self;
    [self.web setValue:@NO forKey:@"drawsBackground"];

    self.fenster = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 1400, 860)
        styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        backing:NSBackingStoreBuffered defer:NO];
    self.fenster.title = @"Open Libary";
    self.fenster.backgroundColor = [NSColor colorWithRed:0.086 green:0.082 blue:0.071 alpha:1];
    self.fenster.contentView = self.web;
    [self.fenster setFrameAutosaveName:@"Open Libary"];
    self.fenster.delegate = self;
    [self.fenster center];
    [self.fenster makeKeyAndOrderFront:nil];
    [self menueBauen];

    // die letzte Mausbewegung mit gedrückter Taste: daraus startet die Ziehsitzung
    __weak Fenster *ich = self;
    [NSEvent addLocalMonitorForEventsMatchingMask:(NSEventMaskLeftMouseDown | NSEventMaskLeftMouseDragged)
                                          handler:^NSEvent *(NSEvent *e) { ich.letzteMaus = e; return e; }];
    [self serverStarten];
}

- (void)menueBauen {
    NSMenu *haupt = [[NSMenu alloc] init];
    NSMenuItem *appEintrag = [[NSMenuItem alloc] init]; [haupt addItem:appEintrag];
    NSMenu *appMenue = [[NSMenu alloc] init];
    [appMenue addItemWithTitle:@"Open Libary beenden" action:@selector(terminate:) keyEquivalent:@"q"];
    appEintrag.submenu = appMenue;
    NSMenuItem *bearbeiten = [[NSMenuItem alloc] init]; [haupt addItem:bearbeiten];
    NSMenu *bm = [[NSMenu alloc] initWithTitle:@"Bearbeiten"];
    [bm addItemWithTitle:@"Ausschneiden" action:@selector(cut:) keyEquivalent:@"x"];
    [bm addItemWithTitle:@"Kopieren" action:@selector(copy:) keyEquivalent:@"c"];
    [bm addItemWithTitle:@"Einsetzen" action:@selector(paste:) keyEquivalent:@"v"];
    [bm addItemWithTitle:@"Alles auswählen" action:@selector(selectAll:) keyEquivalent:@"a"];
    bearbeiten.submenu = bm;
    NSMenuItem *ansicht = [[NSMenuItem alloc] init]; [haupt addItem:ansicht];
    NSMenu *am = [[NSMenu alloc] initWithTitle:@"Ansicht"];
    NSMenuItem *neu = [am addItemWithTitle:@"Neu laden" action:@selector(neuLaden) keyEquivalent:@"r"];
    neu.target = self;
    ansicht.submenu = am;
    [NSApp setMainMenu:haupt];
}

- (void)neuLaden {
    [self.web loadRequest:[NSURLRequest requestWithURL:[NSURL URLWithString:ADRESSE]
                                           cachePolicy:NSURLRequestReloadIgnoringLocalCacheData timeoutInterval:5]];
}

- (void)serverStarten {
    NSFileManager *fm = [NSFileManager defaultManager];
    BOOL paket = [fm isExecutableFileAtPath:paketPython()] && [fm fileExistsAtPath:paketCode()];
    [self seite:@"Open Libary startet …" text:(paket
        ? @"Beim ersten Start fragt macOS nach dem Zugriff auf Ordner: <b>Erlauben</b>, dann geht es weiter."
        : @"Fragt macOS nach dem Zugriff auf „Dokumente“: <b>Erlauben</b>, dann geht es weiter.")];
    dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{
        NSTask *p = [[NSTask alloc] init];
        p.executableURL = [NSURL fileURLWithPath:@"/bin/bash"];
        if (paket) {
            // im Hintergrund, bleibt nach dem Schließen des Fensters. --ersetzen: derselbe Stand läuft schon -> nichts,
            // alter Stand dieser App (Update) -> ersetzen. -B: keine .pyc ins signierte Paket schreiben (sie liegen
            // vorkompiliert bei), -E -s: keine PYTHON*-Variablen und keine Nutzer-Pakete von außen
            NSString *befehl = @"nohup \"$OL_PY\" -B -E -s -X utf8 \"$OL_CODE\" --port 8765 --ohne-browser --ersetzen "
                               @">> \"$OL_LOG\" 2>&1 < /dev/null &";
            p.arguments = @[@"-c", befehl];
            NSMutableDictionary *umgebung = [[[NSProcessInfo processInfo] environment] mutableCopy];
            umgebung[@"OL_PY"] = paketPython();
            umgebung[@"OL_CODE"] = paketCode();
            umgebung[@"OL_LOG"] = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Logs/openlibary.log"];
            umgebung[@"OPENLIBARY_DATEN"] = datenOrdner();
            umgebung[@"OPENLIBARY_PAKET"] = @"1";
            p.environment = umgebung;
        } else {
            p.arguments = @[@"-lc", [NSString stringWithFormat:@"\"%@\" --nur-server", starterPfad()]];
        }
        NSError *f = nil;
        if ([p launchAndReturnError:&f]) [p waitUntilExit];
        [self warten];
    });
}

// Server abfragen, bis er antwortet (beim ersten Start wartet er auf die macOS-Freigabe), höchstens 5 Minuten
- (void)warten {
    self.versuche += 1;
    NSMutableURLRequest *anfrage = [NSMutableURLRequest requestWithURL:
        [NSURL URLWithString:[ADRESSE stringByAppendingString:@"api/stand"]]];
    anfrage.timeoutInterval = 3;
    [[[NSURLSession sharedSession] dataTaskWithRequest:anfrage completionHandler:
        ^(NSData *d, NSURLResponse *antwort, NSError *e) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if ([antwort isKindOfClass:[NSHTTPURLResponse class]] && ((NSHTTPURLResponse *)antwort).statusCode == 200) {
                [self neuLaden];
            } else if (self.versuche > 100) {
                [self seite:@"Der Open Libary-Server startet nicht."
                       text:@"Den Grund nennt das Protokoll: <code>~/Library/Logs/openlibary.log</code> (Konsole-App oder Finder)."];
            } else {
                dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC),
                               dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{ [self warten]; });
            }
        });
    }] resume];
}

- (void)seite:(NSString *)titel text:(NSString *)text {
    NSString *html = [NSString stringWithFormat:
        @"<body style='background:#161512;color:#e9e4d8;font:14px -apple-system,sans-serif;padding:40px'><b>%@</b><br>%@</body>",
        titel, text];
    [self.web loadHTMLString:html baseURL:nil];
}

// Fremde Adressen nie im Fenster der App (es hat die Brücken ziehen, finder, ordner): in den Standardbrowser
- (void)webView:(WKWebView *)w decidePolicyForNavigationAction:(WKNavigationAction *)a
    decisionHandler:(void (^)(WKNavigationActionPolicy))entscheiden {
    NSURL *u = a.request.URL;
    BOOL hauptRahmen = a.targetFrame == nil || a.targetFrame.isMainFrame;
    if (u && hauptRahmen && !([u.host isEqualToString:@"127.0.0.1"] || [u.scheme isEqualToString:@"about"]
                              || [u.scheme isEqualToString:@"data"])) {
        [[NSWorkspace sharedWorkspace] openURL:u];
        entscheiden(WKNavigationActionPolicyCancel);
        return;
    }
    entscheiden(WKNavigationActionPolicyAllow);
}

// Links mit target=_blank (Quellen, Freesound-Login, Looperman) im Standardbrowser öffnen
- (WKWebView *)webView:(WKWebView *)w createWebViewWithConfiguration:(WKWebViewConfiguration *)c
   forNavigationAction:(WKNavigationAction *)a windowFeatures:(WKWindowFeatures *)f {
    if (a.request.URL) [[NSWorkspace sharedWorkspace] openURL:a.request.URL];
    return nil;
}

// Eigene Ordner: macOS-Dialog als Blatt am Fenster, die gewählten Pfade gehen an window.ordnerGewaehlt der Seite
- (void)ordnerWaehlen {
    NSOpenPanel *panel = [NSOpenPanel openPanel];
    panel.canChooseDirectories = YES;
    panel.canChooseFiles = NO;
    panel.allowsMultipleSelection = YES;
    panel.prompt = @"Hinzufügen";
    panel.message = @"Sample-Ordner für Open Libary wählen (Drum Kits, Packs, eigene Aufnahmen)";
    [panel beginSheetModalForWindow:self.fenster completionHandler:^(NSModalResponse antwort) {
        if (antwort != NSModalResponseOK) return;
        NSMutableArray *pfade = [NSMutableArray array];
        for (NSURL *u in panel.URLs) [pfade addObject:u.path];
        NSData *json = [NSJSONSerialization dataWithJSONObject:pfade options:0 error:nil];
        if (!json) return;
        NSString *text = [[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding];
        [self.web evaluateJavaScript:[NSString stringWithFormat:@"window.ordnerGewaehlt && window.ordnerGewaehlt(%@)", text]
                   completionHandler:nil];
    }];
}

- (void)userContentController:(WKUserContentController *)u didReceiveScriptMessage:(WKScriptMessage *)m {
    if ([m.name isEqualToString:@"ordner"]) { [self ordnerWaehlen]; return; }
    if ([m.name isEqualToString:@"kopieren"] && [m.body isKindOfClass:[NSString class]]) {
        [[NSPasteboard generalPasteboard] clearContents];
        [[NSPasteboard generalPasteboard] setString:m.body forType:NSPasteboardTypeString];
        return;
    }
    // Im Finder zeigen: die App selbst, weil "open -R" aus einem im Hintergrund gestarteten Server still nichts tut
    if ([m.name isEqualToString:@"finder"] && [m.body isKindOfClass:[NSString class]]) {
        NSString *pfad = m.body;
        BOOL ordner = NO;
        if (![[NSFileManager defaultManager] fileExistsAtPath:pfad isDirectory:&ordner]) return;
        if (ordner && ![pfad hasSuffix:@".app"]) [[NSWorkspace sharedWorkspace] openURL:[NSURL fileURLWithPath:pfad]];
        else [[NSWorkspace sharedWorkspace] activateFileViewerSelectingURLs:@[[NSURL fileURLWithPath:pfad]]];
        return;
    }
    if (![m.name isEqualToString:@"ziehen"] || ![m.body isKindOfClass:[NSArray class]] || !self.letzteMaus) return;
    NSMutableArray<NSURL *> *urls = [NSMutableArray array];
    for (id p in (NSArray *)m.body)
        if ([p isKindOfClass:[NSString class]] && [[NSFileManager defaultManager] fileExistsAtPath:p])
            [urls addObject:[NSURL fileURLWithPath:p]];
    if (urls.count == 0) return;
    // Ziehen aus der Netz-Liste lädt erst: ist die Taste inzwischen los, kein Ziehbild, das am Zeiger hängen bliebe
    if (([NSEvent pressedMouseButtons] & 1) == 0) {
        [self.web evaluateJavaScript:@"window.ziehenVerpasst && ziehenVerpasst()" completionHandler:nil];
        return;
    }
    NSPoint ort = [self.web convertPoint:self.letzteMaus.locationInWindow fromView:nil];
    NSMutableArray<NSDraggingItem *> *items = [NSMutableArray array];
    [urls enumerateObjectsUsingBlock:^(NSURL *url, NSUInteger i, BOOL *stop) {
        NSDraggingItem *item = [[NSDraggingItem alloc] initWithPasteboardWriter:url];
        NSImage *bild = [[NSWorkspace sharedWorkspace] iconForFile:url.path];
        bild.size = NSMakeSize(32, 32);
        [item setDraggingFrame:NSMakeRect(ort.x - 16 + i * 4, ort.y - 16 - i * 4, 32, 32) contents:bild];
        [items addObject:item];
    }];
    [self.web beginDraggingSessionWithItems:items event:self.letzteMaus source:self];
}

- (NSDragOperation)draggingSession:(NSDraggingSession *)s sourceOperationMaskForDraggingContext:(NSDraggingContext)c {
    return c == NSDraggingContextOutsideApplication ? (NSDragOperationCopy | NSDragOperationLink | NSDragOperationGeneric)
                                                    : NSDragOperationNone;
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)s { return YES; }

@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        Fenster *fenster = [[Fenster alloc] init];
        app.delegate = fenster;
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        [app activateIgnoringOtherApps:YES];
        [app run];
    }
    return 0;
}
