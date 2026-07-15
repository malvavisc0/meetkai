"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
var __metadata = (this && this.__metadata) || function (k, v) {
    if (typeof Reflect === "object" && typeof Reflect.metadata === "function") return Reflect.metadata(k, v);
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.WEBJSEngineMediaProcessor = exports.WhatsappSessionWebJSCore = void 0;
const common_1 = require("@nestjs/common");
const session_abc_1 = require("../../abc/session.abc");
const session_noweb_core_1 = require("../noweb/session.noweb.core");
const ack_webjs_1 = require("./ack.webjs");
const groups_webjs_1 = require("./groups.webjs");
const presence_1 = require("./presence");
const WebjsClientCore_1 = require("./WebjsClientCore");
const WPage_1 = require("./WPage");
const WAMimeType_1 = require("../../media/WAMimeType");
const files_1 = require("../../../utils/files");
const exceptions_1 = require("../../exceptions");
const LottieMediaProcessorWrapper_1 = require("../../media/LottieMediaProcessorWrapper");
const QR_1 = require("../../QR");
const acks_1 = require("../../utils/acks");
const ids_1 = require("../../utils/ids");
const reactive_1 = require("../../utils/reactive");
const helpers_1 = require("../../../helpers");
const channels_dto_1 = require("../../../structures/channels.dto");
const chats_dto_1 = require("../../../structures/chats.dto");
const chatting_dto_1 = require("../../../structures/chatting.dto");
const vcard_1 = require("../../vcard");
const contacts_dto_1 = require("../../../structures/contacts.dto");
const enums_dto_1 = require("../../../structures/enums.dto");
const groups_dto_1 = require("../../../structures/groups.dto");
const labels_dto_1 = require("../../../structures/labels.dto");
const pagination_dto_1 = require("../../../structures/pagination.dto");
const status_dto_1 = require("../../../structures/status.dto");
const Paginator_1 = require("../../../utils/Paginator");
const promiseTimeout_1 = require("../../../utils/promiseTimeout");
const SingleDelayedJobRunner_1 = require("../../../utils/SingleDelayedJobRunner");
const tmpdir_1 = require("../../../utils/tmpdir");
const lodash = require("lodash");
const path = require("path");
const puppeteer_1 = require("puppeteer");
const rxjs_1 = require("rxjs");
const operators_1 = require("rxjs/operators");
const whatsapp_web_js_1 = require("whatsapp-web.js");
const structures_1 = require("whatsapp-web.js/src/structures");
const types_1 = require("./types");
const WebJSAuthFactory_1 = require("./WebJSAuthFactory");
const jids_1 = require("../../utils/jids");
const activity_1 = require("../../abc/activity");
const const_1 = require("../const");
const env_1 = require("../../env");
const chrome_1 = require("../../utils/chrome");
const processes_1 = require("../../utils/processes");
const version_1 = require("../../../version");
class WhatsappSessionWebJSCore extends session_abc_1.WhatsappSession {
    constructor(config) {
        super(config);
        this.START_ATTEMPT_DELAY_SECONDS = 2;
        this.authFactory = new WebJSAuthFactory_1.WebJSAuthFactory();
        this.engine = enums_dto_1.WAHAEngine.WEBJS;
        this.lastQRDate = null;
        this.callRejected$ = new rxjs_1.Subject();
        this.qr = new QR_1.QR();
        this.shouldRestart = true;
        this.startDelayedJob = new SingleDelayedJobRunner_1.SingleDelayedJobRunner('start-engine', this.START_ATTEMPT_DELAY_SECONDS * enums_dto_1.SECOND, this.logger);
        this.engineStateCheckDelayedJob = new SingleDelayedJobRunner_1.SingleDelayedJobRunner('engine-state-check', 2 * enums_dto_1.SECOND, this.logger);
    }
    getClassDirName() {
        return __dirname;
    }
    getClientOptions() {
        var _a, _b, _c, _d, _e, _f, _g, _h;
        const path = this.getClassDirName();
        const webVersion = ((_a = this.engineConfig) === null || _a === void 0 ? void 0 : _a.webVersion) || '2.3000.1018072227-alpha';
        const cacheType = ((_b = this.engineConfig) === null || _b === void 0 ? void 0 : _b.cacheType) || 'none';
        this.logger.info(`Using cache type: '${cacheType}'`);
        if (cacheType === 'local') {
            this.logger.info(`Using web version: '${webVersion}'`);
        }
        const args = this.getBrowserArgsForPuppeteer();
        args.push(...this.engineConfig.puppeteerArgs);
        args.unshift(`--a-waha-timestamp=${new Date()}`);
        args.unshift(`--a-waha-session=${this.name}`);
        const deviceName = (_e = (_d = (_c = this.sessionConfig) === null || _c === void 0 ? void 0 : _c.client) === null || _d === void 0 ? void 0 : _d.deviceName) !== null && _e !== void 0 ? _e : env_1.WAHA_CLIENT_DEVICE_NAME;
        const browserName = (_h = (_g = (_f = this.sessionConfig) === null || _f === void 0 ? void 0 : _f.client) === null || _g === void 0 ? void 0 : _g.browserName) !== null && _h !== void 0 ? _h : env_1.WAHA_CLIENT_BROWSER_NAME;
        return {
            puppeteer: {
                protocolTimeout: 300000,
                headless: true,
                executablePath: this.getBrowserExecutablePath(),
                args: args,
                dumpio: this.isDebugEnabled(),
            },
            userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
            deviceName: deviceName,
            browserName: browserName,
            webVersion: webVersion,
            webVersionCache: {
                type: cacheType,
                path: path,
                strict: true,
            },
        };
    }
    getUserDataDir() {
        const base = process.env.WAHA_LOCAL_STORE_BASE_DIR || './.sessions';
        return `${base}/webjs/default/session-${this.name}`;
    }
    async buildClient() {
        const authStrategy = await this.authFactory.buildAuth(this.sessionStore, this.name, this.loggerBuilder);
        const clientOptions = this.getClientOptions();
        clientOptions.authStrategy = authStrategy;
        this.addProxyConfig(clientOptions);
        return new WebjsClientCore_1.WebjsClientCore(clientOptions, this.getWebjsTagsFlag());
    }
    getWebjsTagsFlag() {
        var _a, _b;
        return !!((_b = (_a = this.sessionConfig) === null || _a === void 0 ? void 0 : _a.webjs) === null || _b === void 0 ? void 0 : _b.tagsEventsOn);
    }
    restartClient() {
        if (!this.shouldRestart) {
            this.logger.debug('Should not restart the client, ignoring restart request');
            this.end().catch((error) => {
                this.logger.error(error, 'Failed to end() the client');
            });
            return;
        }
        this.startDelayedJob.schedule(async () => {
            if (!this.shouldRestart) {
                this.logger.warn('Should not restart the client, ignoring restart request');
                return;
            }
            await this.end();
            await this.start();
        });
    }
    addProxyConfig(clientOptions) {
        var _a, _b, _c, _d, _e, _f;
        if (((_a = this.proxyConfig) === null || _a === void 0 ? void 0 : _a.server) !== undefined) {
            clientOptions.puppeteer.args.push(`--proxy-server=${(_b = this.proxyConfig) === null || _b === void 0 ? void 0 : _b.server}`);
            if (((_c = this.proxyConfig) === null || _c === void 0 ? void 0 : _c.username) && ((_d = this.proxyConfig) === null || _d === void 0 ? void 0 : _d.password)) {
                clientOptions.proxyAuthentication = {
                    username: (_e = this.proxyConfig) === null || _e === void 0 ? void 0 : _e.username,
                    password: (_f = this.proxyConfig) === null || _f === void 0 ? void 0 : _f.password,
                };
            }
        }
    }
    async init() {
        this.shouldRestart = true;
        this.whatsapp = await this.buildClient();
        this.whatsapp
            .initialize()
            .then(() => {
            this.whatsapp.pupBrowser.on('disconnected', () => {
                if (this.shouldRestart) {
                    this.logger.error('The browser has been disconnected');
                }
                else {
                    this.logger.info('The browser has been disconnected');
                }
                this.failed();
            });
            this.whatsapp.pupPage.on('close', () => {
                this.logger.error('The WhatsApp Web page has been closed');
                this.failed();
            });
            this.whatsapp.events.on(WPage_1.PAGE_CALL_ERROR_EVENT, (event) => {
                if (event.error instanceof puppeteer_1.ProtocolError) {
                    if (this.shouldIgnoreProtocolError(event.error)) {
                        this.logger.warn(`ProtocolError when calling page method: ${String(event.method)}, ignoring...`);
                        this.logger.warn(event.error);
                        return;
                    }
                    this.logger.error(`ProtocolError when calling page method: ${String(event.method)}, restarting client...`);
                    this.logger.error(event.error);
                    this.failed();
                }
            });
            if (this.isDebugEnabled()) {
                this.logger.debug("Logging 'console' event for web page");
                this.whatsapp.pupPage.on('console', (msg) => this.logger.debug(`WEBJS page log: ${msg.text()}`));
                this.whatsapp.pupPage.evaluate(() => console.log(`url is ${location.href}`));
            }
        })
            .catch((error) => {
            this.logger.error(error);
            this.failed();
            return;
        });
        if (this.isDebugEnabled()) {
            this.listenEngineEventsInDebugMode();
        }
        this.listenConnectionEvents();
        this.subscribeEngineEvents2();
    }
    async start() {
        await (0, processes_1.killProcessesByPatterns)([version_1.IsChrome ? 'chrome' : 'chromium', `--a-waha-session=${this.name}`], 'SIGKILL', this.logger);
        await (0, chrome_1.removeSingletonFiles)(this.getUserDataDir());
        this.status = enums_dto_1.WAHASessionStatus.STARTING;
        await this.init().catch((err) => {
            this.logger.error('Failed to start the client');
            this.logger.error(err, err.stack);
            this.failed();
        });
        return this;
    }
    async stop() {
        this.cleanupPresenceTimeout();
        this.shouldRestart = false;
        this.status = enums_dto_1.WAHASessionStatus.STOPPED;
        this.stopEvents();
        this.startDelayedJob.cancel();
        this.mediaManager.close();
        await this.end();
    }
    failed() {
        this.status = enums_dto_1.WAHASessionStatus.FAILED;
        this.restartClient();
    }
    shouldIgnoreProtocolError(error) {
        var _a;
        const message = (_a = error === null || error === void 0 ? void 0 : error.message) !== null && _a !== void 0 ? _a : String(error !== null && error !== void 0 ? error : '');
        return message.includes('Network.getResponseBody');
    }
    async unpair() {
        this.unpairing = true;
        this.shouldRestart = false;
        await this.whatsapp.unpair();
        await (0, promiseTimeout_1.sleep)(2000);
    }
    async end() {
        var _a, _b, _c, _d, _e, _f, _g;
        this.cleanupPresenceTimeout();
        this.presence = null;
        this.engineStateCheckDelayedJob.cancel();
        (_a = this.whatsapp) === null || _a === void 0 ? void 0 : _a.removeAllListeners();
        (_c = (_b = this.whatsapp) === null || _b === void 0 ? void 0 : _b.pupBrowser) === null || _c === void 0 ? void 0 : _c.removeAllListeners();
        (_e = (_d = this.whatsapp) === null || _d === void 0 ? void 0 : _d.pupPage) === null || _e === void 0 ? void 0 : _e.removeAllListeners();
        try {
            await (0, promiseTimeout_1.waitUntil)(async () => {
                const result = !!this.whatsapp.pupBrowser;
                this.logger.debug(`Browser is ready to be closed: ${result}`);
                return result;
            }, 1000, 10000);
            this.logger.debug('Successfully waited for browser to be ready for closing');
        }
        catch (error) {
            this.logger.error(error, 'Failed while waiting for browser to be ready for closing');
        }
        try {
            await ((_f = this.whatsapp) === null || _f === void 0 ? void 0 : _f.destroy());
            this.logger.debug('Successfully destroyed whatsapp client');
        }
        catch (error) {
            this.logger.error(error, 'Failed to destroy whatsapp client');
        }
        try {
            const strategy = (_g = this.whatsapp) === null || _g === void 0 ? void 0 : _g.authStrategy;
            await (strategy === null || strategy === void 0 ? void 0 : strategy.destroy());
            this.logger.debug('Successfully destroyed auth strategy');
        }
        catch (error) {
            this.logger.error(error, 'Failed to destroy auth strategy');
        }
    }
    getSessionMeInfo() {
        var _a;
        const clientInfo = (_a = this.whatsapp) === null || _a === void 0 ? void 0 : _a.info;
        if (!clientInfo) {
            return null;
        }
        const wid = clientInfo.wid;
        return {
            id: wid === null || wid === void 0 ? void 0 : wid._serialized,
            pushName: clientInfo === null || clientInfo === void 0 ? void 0 : clientInfo.pushname,
        };
    }
    listenEngineEventsInDebugMode() {
        for (const key in whatsapp_web_js_1.Events) {
            const event = whatsapp_web_js_1.Events[key];
            this.whatsapp.on(event, (...data) => {
                const log = { event: event, data: data };
                this.logger.debug({ event: log }, `WEBJS event`);
            });
        }
    }
    listenConnectionEvents() {
        this.whatsapp.on(whatsapp_web_js_1.Events.QR_RECEIVED, async (qr) => {
            this.logger.debug('QR received');
            this.qr.save(qr);
            this.printQR(this.qr);
            this.status = enums_dto_1.WAHASessionStatus.SCAN_QR_CODE;
            this.lastQRDate = new Date();
        });
        this.whatsapp.on(whatsapp_web_js_1.Events.READY, () => {
            this.qr.save('');
            this.logger.info(`Session '${this.name}' is ready!`);
        });
        this.whatsapp.on(whatsapp_web_js_1.Events.READY, async () => {
            try {
                const hidden = await this.whatsapp.hideUXFreshLook();
                if (hidden) {
                    this.logger.info('"Fresh look" modal has been hidden');
                }
            }
            catch (err) {
                this.logger.warn('Failed to hide "Fresh look" modal');
                this.logger.warn(err, err.stack);
            }
        });
        this.whatsapp.on(whatsapp_web_js_1.Events.AUTHENTICATED, async (args) => {
            this.status = enums_dto_1.WAHASessionStatus.WORKING;
            this.qr.save('');
            this.logger.info({ args: args }, `Session has been authenticated!`);
            await (0, promiseTimeout_1.sleep)(3000);
            if (!this.whatsapp.info) {
                for (let attempt = 0; attempt < 3; attempt++) {
                    await this.loadClientInfo().catch((error) => this.logger.error(error, `Failed to load client info, attempt ${attempt + 1}`));
                    if (this.whatsapp.info) {
                        break;
                    }
                    await (0, promiseTimeout_1.sleep)(3000);
                }
            }
        });
        this.whatsapp.on(whatsapp_web_js_1.Events.AUTHENTICATION_FAILURE, (args) => {
            this.qr.save('');
            this.shouldRestart = false;
            this.logger.info({ args: args }, `Session has failed to authenticate!`);
            this.failed();
        });
        this.whatsapp.on(whatsapp_web_js_1.Events.DISCONNECTED, (args) => {
            if (args === 'LOGOUT') {
                this.logger.warn({ args: args }, `Session has been logged out!`);
                this.shouldRestart = false;
            }
            this.qr.save('');
            this.logger.info({ args: args }, `Session has been disconnected!`);
            this.failed();
        });
        this.whatsapp.on(whatsapp_web_js_1.Events.STATE_CHANGED, (state) => {
            const badStates = [whatsapp_web_js_1.WAState.OPENING, whatsapp_web_js_1.WAState.TIMEOUT];
            const log = this.logger.child({ state: state, event: 'change_state' });
            log.info('Session engine state changed');
            if (!badStates.includes(state)) {
                return;
            }
            log.info(`Session state changed to bad state, waiting for recovery...`);
            this.engineStateCheckDelayedJob.schedule(async () => {
                if (this.startDelayedJob.scheduled) {
                    log.info('Session is restarting already, skip check.');
                    return;
                }
                if (!this.whatsapp) {
                    log.warn('Session is not initialized, skip recovery.');
                    return;
                }
                const currentState = await this.whatsapp.getState().catch((error) => {
                    log.error('Failed to get current state');
                    log.error(error, error.stack);
                    return null;
                });
                log.setBindings({ currentState: currentState });
                if (!currentState) {
                    log.warn('Session has no current state, restarting...');
                    this.restartClient();
                    return;
                }
                else if (badStates.includes(currentState)) {
                    log.info('Session is still in bad state, restarting...');
                    this.restartClient();
                    return;
                }
                log.info('Session has recovered, no need to restart.');
            });
        });
    }
    async loadClientInfo() {
        const data = await this.whatsapp.pupPage.evaluate(() => {
            const WAWebUserPrefsMeUser = window.require('WAWebUserPrefsMeUser');
            return Object.assign(Object.assign({}, window.require('WAWebConnModel').Conn.serialize()), { wid: WAWebUserPrefsMeUser.getMaybeMePnUser() ||
                    WAWebUserPrefsMeUser.getMaybeMeLidUser() });
        });
        this.whatsapp.info = data;
    }
    async browserTrace(query) {
        const tmpdir = new tmpdir_1.TmpDir(this.logger, `waha-browser-trace-${this.name}-`, (10 * query.seconds + 120) * 1000);
        const page = this.whatsapp.pupPage;
        return await tmpdir.use(async (dir) => {
            this.logger.info({ query }, `Starting browser tracing...`);
            const filepath = path.join(dir, 'trace.json');
            await page.tracing.start({ path: filepath });
            await (0, promiseTimeout_1.sleep)(query.seconds * 1000);
            await page.tracing.stop();
            this.logger.info(`Browser tracing finished, saved to ${filepath}`);
            return filepath;
        });
    }
    getQR() {
        return this.qr;
    }
    async requestCode(phoneNumber, method, params) {
        const code = await this.whatsapp.requestPairingCode(phoneNumber, true);
        const parts = (0, helpers_1.splitAt)(code, 4);
        const codeRepr = parts.join('-');
        this.logger.debug(`Your code: ${codeRepr}`);
        return { code: codeRepr };
    }
    async getScreenshot() {
        const screenshot = await this.whatsapp.pupPage.screenshot({
            encoding: 'binary',
        });
        return screenshot;
    }
    async checkNumberStatus(request) {
        let phone = request.phone.split('@')[0];
        phone = phone.replace(/\+/g, '');
        const result = await this.whatsapp.getNumberId(phone);
        if (!result) {
            return {
                numberExists: false,
            };
        }
        return {
            numberExists: true,
            chatId: result._serialized,
        };
    }
    async setProfileName(name) {
        await this.whatsapp.setPushName(name);
        return true;
    }
    async setProfileStatus(status) {
        await this.whatsapp.setStatus(status);
        return true;
    }
    async fileToMedia(file) {
        if ('url' in file) {
            const mediaOptions = { unsafeMime: true };
            const media = await whatsapp_web_js_1.MessageMedia.fromUrl(file.url, mediaOptions);
            media.mimetype = file.mimetype || media.mimetype;
            media.filename = file.filename || media.filename;
            return media;
        }
        return new whatsapp_web_js_1.MessageMedia(file.mimetype, file.data, file.filename);
    }
    async setProfilePicture(file) {
        const media = await this.fileToMedia(file);
        return await this.whatsapp.setProfilePicture(media);
    }
    async deleteProfilePicture() {
        return await this.whatsapp.deleteProfilePicture();
    }
    async setGroupPicture(id, file) {
        const media = await this.fileToMedia(file);
        const groupChat = (await this.whatsapp.getChatById(id));
        return await groupChat.setPicture(media);
    }
    async deleteGroupPicture(id) {
        const groupChat = (await this.whatsapp.getChatById(id));
        return await groupChat.deletePicture();
    }
    async rejectCall(from, id) {
        const peerJid = (0, jids_1.normalizeJid)(this.ensureSuffix(from));
        const call = new structures_1.Call(this.whatsapp, null);
        call.id = id;
        call.from = peerJid;
        await call.reject();
        this.callRejected$.next(this.toRejectedCallData(peerJid, id));
    }
    sendText(request) {
        const options = this.getMessageOptions(request);
        return this.whatsapp.sendMessage(this.ensureSuffix(request.chatId), request.text, options);
    }
    deleteMessage(chatId, messageId) {
        const message = this.recreateMessage(messageId);
        return message.delete(true);
    }
    editMessage(chatId, messageId, request) {
        const message = this.recreateMessage(messageId);
        const options = {
            mentions: request.mentions,
            linkPreview: request.linkPreview,
        };
        return message.edit(request.text, options);
    }
    async sendContactVCard(request) {
        const chatId = this.ensureSuffix(request.chatId);
        const vcards = request.contacts.map((el) => (0, vcard_1.toVcardV3)(el));
        const options = this.getMessageOptions(request);
        if (vcards.length <= 1) {
            const vcard = vcards[0] || '';
            return this.whatsapp.sendMessage(chatId, vcard, options);
        }
        const extra = {
            type: 'multi_vcard',
            vcardList: vcards.map((v) => ({ vcard: v })),
            body: null,
        };
        return this.whatsapp.sendMessage(chatId, '', Object.assign(Object.assign({}, options), { extra }));
    }
    async reply(request) {
        const options = this.getMessageOptions(request);
        return this.whatsapp.sendMessage(this.ensureSuffix(request.chatId), request.text, options);
    }
    async sendPoll(request) {
        const poll = new whatsapp_web_js_1.Poll(request.poll.name, request.poll.options, {
            allowMultipleAnswers: request.poll.multipleAnswers,
            messageSecret: undefined,
        });
        const options = this.getMessageOptions(request);
        return this.whatsapp.sendMessage(this.ensureSuffix(request.chatId), poll, options);
    }
    async sendFile(request) {
        const media = await this.fileToMedia(request.file);
        if (!media.mimetype) {
            media.mimetype = await (0, files_1.detectMimetype)(Buffer.from(media.data, 'base64'));
        }
        let options = this.getMessageOptions(request);
        options = Object.assign(Object.assign({}, options), { sendMediaAsDocument: true, caption: request.caption });
        return this.whatsapp.sendMessage(this.ensureSuffix(request.chatId), media, options);
    }
    async sendImage(request) {
        const media = await this.fileToMedia(request.file);
        media.mimetype = media.mimetype || WAMimeType_1.WAMimeType.IMAGE;
        let options = this.getMessageOptions(request);
        options = Object.assign(Object.assign({}, options), { caption: request.caption });
        return this.whatsapp.sendMessage(this.ensureSuffix(request.chatId), media, options);
    }
    async sendVoice(request) {
        const media = await this.fileToMedia(request.file);
        if (request.convert) {
            await this.convertVoice(media);
        }
        media.mimetype = request.file.mimetype || WAMimeType_1.WAMimeType.VOICE;
        let options = this.getMessageOptions(request);
        options = Object.assign(Object.assign({}, options), { sendAudioAsVoice: true });
        return this.whatsapp.sendMessage(this.ensureSuffix(request.chatId), media, options);
    }
    async sendVideo(request) {
        this.checkBrowserIsChrome();
        const media = await this.fileToMedia(request.file);
        media.mimetype = media.mimetype || WAMimeType_1.WAMimeType.VIDEO;
        if (request.convert) {
            await this.convertVideo(media);
        }
        let options = this.getMessageOptions(request);
        options = Object.assign(Object.assign({}, options), { caption: request.caption });
        return this.whatsapp.sendMessage(this.ensureSuffix(request.chatId), media, options);
    }
    async convertVideo(media) {
        let content = Buffer.from(media.data, 'base64');
        content = await this.mediaConverter.video(content);
        media.data = content.toString('base64');
        media.mimetype = WAMimeType_1.WAMimeType.VIDEO;
        media.filename = null;
        media.filesize = null;
    }
    async convertVoice(media) {
        let content = Buffer.from(media.data, 'base64');
        content = await this.mediaConverter.voice(content);
        media.data = content.toString('base64');
        media.mimetype = WAMimeType_1.WAMimeType.VOICE;
        media.filename = null;
        media.filesize = null;
    }
    async sendPollVote(request) {
        const message = await this.whatsapp.getMessageById(request.pollMessageId);
        if (!message) {
            throw new common_1.UnprocessableEntityException(`Poll message not found: ${request.pollMessageId}`);
        }
        return message.vote(request.votes);
    }
    async sendButtonsReply(request) {
        const options = this.getMessageOptions(request);
        const extra = {
            type: 'buttons_response',
            kind: 'buttonsResponse',
            buttonsResponse: {
                selectedButtonId: request.selectedButtonID,
                selectedDisplayText: request.selectedDisplayText,
                type: 1,
            },
            viewMode: 'VISIBLE',
        };
        options.extra = extra;
        return this.whatsapp.sendMessage(this.ensureSuffix(request.chatId), request.selectedDisplayText, options);
    }
    checkBrowserIsChrome() {
        if (!version_1.IsChrome) {
            const msg = 'Use "devlikeapro/waha:chrome" docker image to send video in WEBJS';
            throw new common_1.UnprocessableEntityException(msg);
        }
    }
    async sendLocation(request) {
        const location = new whatsapp_web_js_1.Location(request.latitude, request.longitude, {
            name: request.title,
        });
        const options = this.getMessageOptions(request);
        return this.whatsapp.sendMessage(this.ensureSuffix(request.chatId), location, options);
    }
    async forwardMessage(request) {
        const forwardMessage = this.recreateMessage(request.messageId);
        const msg = await forwardMessage.forward(this.ensureSuffix(request.chatId));
        return { sent: msg || false };
    }
    async sendSeen(request) {
        const chat = await this.whatsapp.getChatById(this.ensureSuffix(request.chatId));
        await chat.sendSeen();
    }
    async startTyping(request) {
        const chat = await this.whatsapp.getChatById(this.ensureSuffix(request.chatId));
        await chat.sendStateTyping();
    }
    async stopTyping(request) {
        const chat = await this.whatsapp.getChatById(this.ensureSuffix(request.chatId));
        await chat.clearState();
    }
    async setReaction(request) {
        const message = this.recreateMessage(request.messageId);
        return message.react(request.reaction);
    }
    recreateMessage(msgId) {
        const messageId = this.deserializeId(msgId);
        const data = {
            id: messageId,
        };
        return new structures_1.Message(this.whatsapp, data);
    }
    async setStar(request) {
        const message = this.recreateMessage(request.messageId);
        if (request.star) {
            await message.star();
        }
        else {
            await message.unstar();
        }
    }
    getChats(pagination, filter = null) {
        switch (pagination.sortBy) {
            case chats_dto_1.ChatSortField.ID:
                pagination.sortBy = 'id._serialized';
                break;
            case chats_dto_1.ChatSortField.CONVERSATION_TIMESTAMP:
                pagination.sortBy = 't';
                break;
        }
        return this.whatsapp.getChats(pagination, filter);
    }
    async getChatsOverview(pagination, filter) {
        pagination = Object.assign(Object.assign({}, pagination), { sortBy: chats_dto_1.ChatSortField.CONVERSATION_TIMESTAMP, sortOrder: pagination_dto_1.SortOrder.DESC });
        const chats = await this.getChats(pagination, filter);
        const promises = [];
        for (const chat of chats) {
            promises.push(this.fetchChatSummary(chat));
        }
        const result = await Promise.all(promises);
        return result;
    }
    async fetchChatSummary(chat) {
        const picture = await this.getContactProfilePicture(chat.id._serialized, false);
        const lastMessage = chat.lastMessage
            ? this.toWAMessage(chat.lastMessage)
            : null;
        return {
            id: chat.id._serialized,
            name: chat.name || null,
            picture: picture,
            lastMessage: lastMessage,
            _chat: chat,
        };
    }
    async getChatMessages(chatId, query, filter) {
        if (chatId == 'all') {
            throw new exceptions_1.NotImplementedByEngineError("Can not get messages from 'all' in WEBJS");
        }
        const downloadMedia = query.downloadMedia;
        await this.whatsapp.getChatById(this.ensureSuffix(chatId));
        const pagination = query;
        const messages = await this.whatsapp.getMessages(this.ensureSuffix(chatId), filter, pagination);
        const promises = [];
        for (const msg of messages) {
            promises.push(this.processIncomingMessage(msg, downloadMedia));
        }
        let result = await Promise.all(promises);
        result = result.filter(Boolean);
        return result;
    }
    async readChatMessages(chatId, request) {
        const chat = await this.whatsapp.getChatById(this.ensureSuffix(chatId));
        await chat.sendSeen();
        return { ids: null };
    }
    async getChatMessage(chatId, messageId, query) {
        chatId = this.ensureSuffix(chatId);
        if ((0, jids_1.isJidStatusBroadcast)(chatId) && !messageId.includes('_')) {
            const me = this.getSessionMeInfo();
            const lid = me.lid || (await this.whatsapp.findLIDByPhoneNumber(me.id));
            messageId = (0, ids_1.SerializeMessageKey)({
                fromMe: true,
                id: messageId,
                remoteJid: const_1.Jid.BROADCAST,
                participant: lid,
            });
        }
        let message = null;
        if ((0, jids_1.isLidUser)(chatId) || (0, jids_1.isJidCus)(chatId)) {
            if (!messageId.includes('_')) {
                if (!message) {
                    const id = (0, ids_1.SerializeMessageKey)({
                        fromMe: true,
                        id: messageId,
                        remoteJid: chatId,
                    });
                    message = await this.whatsapp.getMessageById(id);
                }
                if (!message) {
                    const id = (0, ids_1.SerializeMessageKey)({
                        fromMe: false,
                        id: messageId,
                        remoteJid: chatId,
                    });
                    message = await this.whatsapp.getMessageById(id);
                }
            }
        }
        if (!message) {
            message = await this.whatsapp.getMessageById(messageId);
        }
        if (!message)
            return null;
        if ((0, jids_1.isJidGroup)(message.id.remote) ||
            (0, jids_1.isJidStatusBroadcast)(message.id.remote)) {
            message.rawData.receipts = await message.getInfo().catch((error) => {
                this.logger.error({ error: error, msg: message.id._serialized }, 'Failed to get receipts');
                return null;
            });
        }
        return await this.processIncomingMessage(message, query.downloadMedia);
    }
    async pinMessage(chatId, messageId, duration) {
        const message = await this.whatsapp.getMessageById(messageId);
        return message.pin(duration);
    }
    async unpinMessage(chatId, messageId) {
        const message = await this.whatsapp.getMessageById(messageId);
        return message.unpin();
    }
    async deleteChat(chatId) {
        const chat = await this.whatsapp.getChatById(this.ensureSuffix(chatId));
        return chat.delete();
    }
    async clearMessages(chatId) {
        const chat = await this.whatsapp.getChatById(chatId);
        return chat.clearMessages();
    }
    chatsArchiveChat(chatId) {
        const id = this.ensureSuffix(chatId);
        return this.whatsapp.archiveChat(id);
    }
    chatsUnarchiveChat(chatId) {
        const id = this.ensureSuffix(chatId);
        return this.whatsapp.unarchiveChat(id);
    }
    chatsUnreadChat(chatId) {
        const id = this.ensureSuffix(chatId);
        return this.whatsapp.markChatUnread(id);
    }
    async getLabels() {
        const labels = await this.whatsapp.getLabels();
        return labels.map(this.toLabel);
    }
    async createLabel(label) {
        const labelId = await this.whatsapp.createLabel(label.name, label.color);
        return {
            id: labelId.toString(),
            name: label.name,
            color: label.color,
            colorHex: labels_dto_1.Label.toHex(label.color),
        };
    }
    async updateLabel(label) {
        return await this.whatsapp.updateLabel(label);
    }
    deleteLabel(label) {
        return this.whatsapp.deleteLabel(label);
    }
    getChatsByLabelId(labelId) {
        return this.whatsapp.getChatsByLabelId(labelId);
    }
    async getChatLabels(chatId) {
        const id = this.ensureSuffix(chatId);
        const labels = await this.whatsapp.getChatLabels(id);
        return labels.map(this.toLabel);
    }
    async putLabelsToChat(chatId, labels) {
        const labelIds = labels.map((label) => label.id);
        const chatIds = [this.ensureSuffix(chatId)];
        await this.whatsapp.addOrRemoveLabels(labelIds, chatIds);
    }
    toLabel(label) {
        const color = label.colorIndex;
        return {
            id: label.id,
            name: label.name,
            color: color,
            colorHex: labels_dto_1.Label.toHex(color),
        };
    }
    async upsertContact(chatId, body) {
        const phoneNumber = chatId.split('@')[0];
        await this.whatsapp.saveOrEditAddressbookContact(phoneNumber, body.firstName, body.lastName, true);
    }
    getContact(query) {
        return this.whatsapp
            .getContactById(this.ensureSuffix(query.contactId))
            .then(this.toWAContact);
    }
    async getContacts(pagination) {
        const contactsWEBJS = await this.whatsapp.getContacts();
        const contacts = contactsWEBJS.map(this.toWAContact);
        const paginator = new Paginator_1.PaginatorInMemory(pagination);
        return paginator.apply(contacts);
    }
    async getContactAbout(query) {
        const contact = await this.whatsapp.getContactById(this.ensureSuffix(query.contactId));
        return { about: await contact.getAbout() };
    }
    async fetchContactProfilePicture(id) {
        const contact = await this.whatsapp.getContactById(this.ensureSuffix(id));
        const url = await contact.getProfilePicUrl();
        return url;
    }
    async blockContact(request) {
        const contact = await this.whatsapp.getContactById(this.ensureSuffix(request.contactId));
        await contact.block();
    }
    async unblockContact(request) {
        const contact = await this.whatsapp.getContactById(this.ensureSuffix(request.contactId));
        await contact.unblock();
    }
    async getAllLids(pagination) {
        return this.whatsapp.getAllLids(pagination);
    }
    async getLidsCount() {
        return this.whatsapp.getLidsCount();
    }
    async findPNByLid(lid) {
        const phoneNumber = await this.whatsapp.findPNByLid(lid);
        return {
            lid: lid,
            pn: phoneNumber,
        };
    }
    async findLIDByPhoneNumber(phoneNumber) {
        const pn = (0, jids_1.toCusFormat)(phoneNumber);
        const lid = await this.whatsapp.findLIDByPhoneNumber(pn);
        return {
            lid: lid,
            pn: pn,
        };
    }
    createGroup(request) {
        const participantIds = request.participants.map((participant) => participant.id);
        return this.whatsapp.createGroup(request.name, participantIds);
    }
    joinGroup(code) {
        return this.whatsapp.acceptInvite(code);
    }
    joinInfoGroup(code) {
        return this.whatsapp.getInviteInfo(code);
    }
    async getInfoAdminsOnly(id) {
        const groupChat = (await this.whatsapp.getChatById(id));
        return {
            adminsOnly: groupChat.groupMetadata.restrict,
        };
    }
    async setInfoAdminsOnly(id, value) {
        const groupChat = (await this.whatsapp.getChatById(id));
        return groupChat.setInfoAdminsOnly(value);
    }
    async getMessagesAdminsOnly(id) {
        const groupChat = (await this.whatsapp.getChatById(id));
        return {
            adminsOnly: groupChat.groupMetadata.announce,
        };
    }
    async setMessagesAdminsOnly(id, value) {
        const groupChat = (await this.whatsapp.getChatById(id));
        return groupChat.setMessagesAdminsOnly(value);
    }
    async getGroups(pagination) {
        const chats = await this.whatsapp.getChats();
        const groups = lodash.filter(chats, (chat) => chat.isGroup);
        switch (pagination.sortBy) {
            case groups_dto_1.GroupSortField.ID:
                pagination.sortBy = 'id._serialized';
                break;
            case groups_dto_1.GroupSortField.SUBJECT:
                pagination.sortBy = 'groupMetadata.subject';
                break;
        }
        const paginator = new Paginator_1.PaginatorInMemory(pagination);
        return paginator.apply(groups);
    }
    removeGroupsFieldParticipant(group) {
        var _a, _b, _c, _d;
        (_a = group.groupMetadata) === null || _a === void 0 ? true : delete _a.participants;
        (_b = group.groupMetadata) === null || _b === void 0 ? true : delete _b.pendingParticipants;
        (_c = group.groupMetadata) === null || _c === void 0 ? true : delete _c.pastParticipants;
        (_d = group.groupMetadata) === null || _d === void 0 ? true : delete _d.membershipApprovalRequests;
    }
    async refreshGroups() {
        return true;
    }
    getGroup(id) {
        return this.whatsapp.getChatById(id);
    }
    async getGroupParticipants(id) {
        const group = (await this.whatsapp.getChatById(id));
        return (0, groups_webjs_1.getParticipants)(group.participants);
    }
    async deleteGroup(id) {
        const groupChat = (await this.whatsapp.getChatById(id));
        return groupChat.delete();
    }
    async leaveGroup(id) {
        const groupChat = (await this.whatsapp.getChatById(id));
        return groupChat.leave();
    }
    async setDescription(id, description) {
        const groupChat = (await this.whatsapp.getChatById(id));
        return groupChat.setDescription(description);
    }
    async setSubject(id, subject) {
        const groupChat = (await this.whatsapp.getChatById(id));
        return groupChat.setSubject(subject);
    }
    async getInviteCode(id) {
        const groupChat = (await this.whatsapp.getChatById(id));
        return groupChat.getInviteCode();
    }
    async revokeInviteCode(id) {
        const groupChat = (await this.whatsapp.getChatById(id));
        await groupChat.revokeInvite();
        return groupChat.getInviteCode();
    }
    async getParticipants(id) {
        const groupChat = (await this.whatsapp.getChatById(id));
        return groupChat.participants;
    }
    async addParticipants(id, request) {
        const groupChat = (await this.whatsapp.getChatById(id));
        const participantIds = request.participants.map((participant) => participant.id);
        return groupChat.addParticipants(participantIds);
    }
    async removeParticipants(id, request) {
        const groupChat = (await this.whatsapp.getChatById(id));
        const participantIds = request.participants.map((participant) => participant.id);
        return groupChat.removeParticipants(participantIds);
    }
    async promoteParticipantsToAdmin(id, request) {
        const groupChat = (await this.whatsapp.getChatById(id));
        const participantIds = request.participants.map((participant) => participant.id);
        return groupChat.promoteParticipants(participantIds);
    }
    async demoteParticipantsToUser(id, request) {
        const groupChat = (await this.whatsapp.getChatById(id));
        const participantIds = request.participants.map((participant) => participant.id);
        return groupChat.demoteParticipants(participantIds);
    }
    async searchChannelsByView(query) {
        const params = {
            view: query.view || 'TRENDING',
            countryCodes: query.countries,
            cursorToken: query.startCursor,
            categories: query.categories,
            limit: query.limit,
        };
        const data = await this.whatsapp.searchChannelsView(params);
        return this.channelsRawDataToResponse(data);
    }
    async searchChannelsByText(query) {
        const params = {
            searchText: query.text,
            cursorToken: query.startCursor,
            categories: query.categories,
            limit: query.limit,
        };
        const data = await this.whatsapp.searchChannelsText(params);
        return this.channelsRawDataToResponse(data);
    }
    channelsRawDataToResponse(data) {
        const pageInfo = data.pageInfo;
        const newsletters = data.newsletters;
        return {
            page: {
                startCursor: pageInfo.startCursor,
                endCursor: pageInfo.endCursor,
                hasNextPage: pageInfo.hasNextPage,
                hasPreviousPage: pageInfo.hasPreviousPage,
            },
            channels: newsletters.map(NewsletterMetadataToChannel),
        };
    }
    async previewChannelMessages(inviteCode, query) {
        const channelMessages = await this.whatsapp.channelFetchMessageByInvite(inviteCode, query.limit);
        const promises = [];
        for (const msg of channelMessages) {
            promises.push(this.WebjsChannelMessageToChannelMessage(msg, query.downloadMedia));
        }
        return await Promise.all(promises);
    }
    async WebjsChannelMessageToChannelMessage(channelMessage, downloadMedia) {
        const message = await this.processIncomingMessage(channelMessage.message, downloadMedia);
        const reactions = {};
        for (const reaction of channelMessage.reactions.sort((x) => -x.count)) {
            reactions[reaction.reaction] = reaction.count;
        }
        return {
            message: message,
            reactions: reactions,
            viewCount: channelMessage.viewCount,
        };
    }
    ChatToChannel(chat) {
        const metadata = chat.channelMetadata;
        let role = metadata.membershipType.toUpperCase();
        if (role === 'VIEWER') {
            role = channels_dto_1.ChannelRole.GUEST;
        }
        return {
            id: chat.id._serialized,
            name: chat.name,
            description: chat.description,
            invite: (0, session_abc_1.getChannelInviteLink)(metadata.inviteCode),
            preview: null,
            picture: null,
            verified: metadata.verified,
            role: role,
            subscribersCount: null,
        };
    }
    ChannelMetadataToChannel(metadata) {
        var _a;
        let role = (_a = metadata.membershipType) === null || _a === void 0 ? void 0 : _a.toUpperCase();
        if (role === 'VIEWER') {
            role = channels_dto_1.ChannelRole.GUEST;
        }
        else if (!role) {
            role = channels_dto_1.ChannelRole.GUEST;
        }
        return {
            id: metadata.id,
            name: metadata.titleMetadata.title,
            description: metadata.descriptionMetadata.description,
            invite: metadata.inviteLink,
            preview: metadata.pictureUrl,
            picture: metadata.pictureUrl,
            verified: metadata.isVerified,
            role: role,
            subscribersCount: metadata.subscribersCount,
        };
    }
    async channelsList(query) {
        const data = await this.whatsapp.getChannels();
        let channels = data.map(this.ChatToChannel);
        if (query.role) {
            channels = channels.filter((channel) => channel.role === query.role);
        }
        channels = channels.filter((channel) => channel.role !== 'GUEST');
        const promises = channels.map(async (channel) => this.whatsapp.getProfilePicUrl(channel.id));
        const pictures = await Promise.all(promises);
        channels = channels.map((channel, index) => {
            channel.picture = pictures[index] || null;
            channel.preview = channel.picture;
            return channel;
        });
        return channels;
    }
    channelsCreateChannel(request) {
        throw new exceptions_1.NotImplementedByEngineError();
    }
    async channelsGetChannel(id) {
        return await this.channelsGetChannelByInviteCode(id);
    }
    async channelsGetChannelByInviteCode(inviteCode) {
        const metadata = await this.whatsapp.getChannelByInviteCode(inviteCode);
        const channel = this.ChannelMetadataToChannel(metadata);
        channel.preview =
            (await this.whatsapp.getProfilePicUrl(channel.id)) || null;
        channel.picture = channel.preview;
        return channel;
    }
    channelsDeleteChannel(id) {
        throw new exceptions_1.NotImplementedByEngineError();
    }
    channelsFollowChannel(id) {
        throw new exceptions_1.NotImplementedByEngineError();
    }
    channelsUnfollowChannel(id) {
        throw new exceptions_1.NotImplementedByEngineError();
    }
    channelsMuteChannel(id) {
        throw new exceptions_1.NotImplementedByEngineError();
    }
    channelsUnmuteChannel(id) {
        throw new exceptions_1.NotImplementedByEngineError();
    }
    async setPresence(presence, chatId) {
        let chat;
        switch (presence) {
            case enums_dto_1.WAHAPresenceStatus.ONLINE:
                await this.whatsapp.sendPresenceAvailable();
                break;
            case enums_dto_1.WAHAPresenceStatus.OFFLINE:
                await this.whatsapp.sendPresenceUnavailable();
                break;
            case enums_dto_1.WAHAPresenceStatus.TYPING:
                await this.maintainPresenceOnline();
                chat = await this.whatsapp.getChatById(chatId);
                await chat.sendStateTyping();
                break;
            case enums_dto_1.WAHAPresenceStatus.RECORDING:
                await this.maintainPresenceOnline();
                chat = await this.whatsapp.getChatById(chatId);
                await chat.sendStateRecording();
                break;
            case enums_dto_1.WAHAPresenceStatus.PAUSED:
                await this.maintainPresenceOnline();
                chat = await this.whatsapp.getChatById(chatId);
                await chat.clearState();
                break;
            default:
                throw new exceptions_1.NotImplementedByEngineError(`WEBJS engine doesn't support '${presence}' presence.`);
        }
        this.presence = presence;
    }
    getPresences() {
        throw new exceptions_1.NotImplementedByEngineError();
    }
    async getPresence(id) {
        const chatId = (0, jids_1.toCusFormat)(id);
        const presences = await this.whatsapp.getPresence(chatId);
        return this.toWahaPresences(chatId, presences);
    }
    async subscribePresence(id) {
        const chatId = (0, jids_1.toCusFormat)(id);
        await this.whatsapp.subscribePresence(chatId);
    }
    toWahaPresences(chatId, data) {
        const presences = data.map((presence) => {
            let status = enums_dto_1.WAHAPresenceStatus.OFFLINE;
            switch (presence.state) {
                case types_1.WAJSPresenceChatStateType.AVAILABLE:
                    status = enums_dto_1.WAHAPresenceStatus.ONLINE;
                    break;
                case types_1.WAJSPresenceChatStateType.UNAVAILABLE:
                    status = enums_dto_1.WAHAPresenceStatus.OFFLINE;
                    break;
                case types_1.WAJSPresenceChatStateType.TYPING:
                    status = enums_dto_1.WAHAPresenceStatus.TYPING;
                    break;
                case types_1.WAJSPresenceChatStateType.RECORDING_AUDIO:
                    status = enums_dto_1.WAHAPresenceStatus.RECORDING;
            }
            return {
                participant: presence.participant,
                lastSeen: presence.lastSeen || null,
                lastKnownPresence: status,
            };
        });
        return {
            id: (0, jids_1.toCusFormat)(chatId),
            presences: presences,
        };
    }
    checkStatusRequest(request) {
        var _a;
        if (request.contacts && ((_a = request.contacts) === null || _a === void 0 ? void 0 : _a.length) > 0) {
            const msg = "WEBJS doesn't accept 'contacts'. Remove the field to send status to all contacts.";
            throw new common_1.UnprocessableEntityException(msg);
        }
    }
    async sendImageStatus(status) {
        this.checkStatusRequest(status);
        const media = await this.fileToMedia(status.file);
        const options = {
            caption: status.caption,
        };
        return this.whatsapp.sendMessage(status_dto_1.BROADCAST_ID, media, options);
    }
    async sendVoiceStatus(status) {
        this.checkStatusRequest(status);
        const media = await this.fileToMedia(status.file);
        if (status.convert) {
            await this.convertVoice(media);
        }
        media.mimetype = status.file.mimetype || WAMimeType_1.WAMimeType.VOICE;
        const options = {
            sendAudioAsVoice: true,
        };
        return this.whatsapp.sendMessage(status_dto_1.BROADCAST_ID, media, options);
    }
    async sendVideoStatus(status) {
        this.checkBrowserIsChrome();
        this.checkStatusRequest(status);
        const media = await this.fileToMedia(status.file);
        if (status.convert) {
            await this.convertVideo(media);
        }
        const options = {
            caption: status.caption,
        };
        return this.whatsapp.sendMessage(status_dto_1.BROADCAST_ID, media, options);
    }
    sendTextStatus(status) {
        this.checkStatusRequest(status);
        const extra = {};
        if (status.font != null) {
            extra.fontStyle = status.font;
        }
        if (status.backgroundColor != null) {
            extra.backgroundColor = status.backgroundColor;
        }
        const options = { extra: extra, linkPreview: status.linkPreview };
        return this.whatsapp.sendMessage(const_1.Jid.BROADCAST, status.text, options);
    }
    async deleteStatus(request) {
        this.checkStatusRequest(request);
        let messageId = request.id;
        if (!request.id.startsWith('true_status@broadcast_')) {
            messageId = `true_status@broadcast_${request.id}`;
        }
        return await this.whatsapp.revokeStatusMessage(messageId);
    }
    subscribeEngineEvents2() {
        this.whatsapp.events.on('message.id', (data) => {
            this.saveSentMessageId(data.id);
        });
        const events = [];
        for (const key in whatsapp_web_js_1.Events) {
            const event = whatsapp_web_js_1.Events[key];
            const event$ = (0, rxjs_1.fromEvent)(this.whatsapp, event);
            events.push(event$.pipe((0, operators_1.map)((data) => {
                return {
                    event: event,
                    data: data,
                };
            })));
        }
        const all$ = (0, rxjs_1.merge)(...events);
        this.events2.get(enums_dto_1.WAHAEvents.ENGINE_EVENT).switch(all$);
        const messageReceived$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.MESSAGE_RECEIVED);
        const messagesFromOthers$ = messageReceived$.pipe((0, rxjs_1.filter)((msg) => { var _a; return this.jids.include((_a = msg === null || msg === void 0 ? void 0 : msg.id) === null || _a === void 0 ? void 0 : _a.remote); }), (0, rxjs_1.mergeMap)((msg) => this.processIncomingMessage(msg, true)), (0, rxjs_1.share)());
        this.events2.get(enums_dto_1.WAHAEvents.MESSAGE).switch(messagesFromOthers$);
        const messageCreate$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.MESSAGE_CREATE);
        const messagesFromAll$ = messageCreate$.pipe((0, rxjs_1.filter)((msg) => { var _a; return this.jids.include((_a = msg === null || msg === void 0 ? void 0 : msg.id) === null || _a === void 0 ? void 0 : _a.remote); }), (0, rxjs_1.mergeMap)((msg) => this.processIncomingMessage(msg, true)), (0, rxjs_1.share)());
        this.events2.get(enums_dto_1.WAHAEvents.MESSAGE_ANY).switch(messagesFromAll$);
        const messageCiphertext$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.MESSAGE_CIPHERTEXT);
        const messagesWaiting$ = messageCiphertext$.pipe((0, rxjs_1.filter)((msg) => { var _a; return this.jids.include((_a = msg === null || msg === void 0 ? void 0 : msg.id) === null || _a === void 0 ? void 0 : _a.remote); }), (0, rxjs_1.mergeMap)((msg) => this.processIncomingMessage(msg, false)), (0, rxjs_1.share)());
        this.events2.get(enums_dto_1.WAHAEvents.MESSAGE_WAITING).switch(messagesWaiting$);
        const messageRevoked$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.MESSAGE_REVOKED_EVERYONE, (after, before) => {
            return { after, before };
        });
        const messagesRevoked$ = messageRevoked$.pipe((0, rxjs_1.filter)((evt) => { var _a, _b, _c, _d; return this.jids.include(((_b = (_a = evt === null || evt === void 0 ? void 0 : evt.after) === null || _a === void 0 ? void 0 : _a.id) === null || _b === void 0 ? void 0 : _b.remote) || ((_d = (_c = evt === null || evt === void 0 ? void 0 : evt.before) === null || _c === void 0 ? void 0 : _c.id) === null || _d === void 0 ? void 0 : _d.remote)); }), (0, operators_1.map)((event) => {
            var _a, _b;
            const afterMessage = event.after ? this.toWAMessage(event.after) : null;
            const beforeMessage = event.before
                ? this.toWAMessage(event.before)
                : null;
            const revokedMessageId = (_b = (_a = afterMessage === null || afterMessage === void 0 ? void 0 : afterMessage._data) === null || _a === void 0 ? void 0 : _a.protocolMessageKey) === null || _b === void 0 ? void 0 : _b.id;
            return {
                after: afterMessage,
                before: beforeMessage,
                revokedMessageId: revokedMessageId,
            };
        }));
        this.events2.get(enums_dto_1.WAHAEvents.MESSAGE_REVOKED).switch(messagesRevoked$);
        const messageReaction$ = (0, rxjs_1.fromEvent)(this.whatsapp, 'message_reaction');
        const messagesReaction$ = messageReaction$.pipe((0, rxjs_1.filter)((reaction) => { var _a; return this.jids.include((_a = reaction === null || reaction === void 0 ? void 0 : reaction.id) === null || _a === void 0 ? void 0 : _a.remote); }), (0, operators_1.map)(this.processMessageReaction.bind(this)), (0, rxjs_1.filter)(Boolean));
        this.events2.get(enums_dto_1.WAHAEvents.MESSAGE_REACTION).switch(messagesReaction$);
        const messageEdit$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.MESSAGE_EDIT, (message, newBody, prevBody) => {
            return { message, newBody, prevBody };
        });
        const messagesEdit$ = messageEdit$.pipe((0, rxjs_1.filter)((event) => { var _a, _b; return this.jids.include((_b = (_a = event === null || event === void 0 ? void 0 : event.message) === null || _a === void 0 ? void 0 : _a.id) === null || _b === void 0 ? void 0 : _b.remote); }), (0, operators_1.map)((event) => {
            var _a, _b;
            const message = this.toWAMessage(event.message);
            return Object.assign(Object.assign({}, message), { body: event.newBody, editedMessageId: (_b = (_a = message._data) === null || _a === void 0 ? void 0 : _a.id) === null || _b === void 0 ? void 0 : _b.id, _data: event });
        }));
        this.events2.get(enums_dto_1.WAHAEvents.MESSAGE_EDITED).switch(messagesEdit$);
        const pollVote$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.VOTE_UPDATE);
        const pollVotes$ = pollVote$.pipe((0, operators_1.map)(this.toPollVotePayload.bind(this)), (0, rxjs_1.filter)(Boolean));
        this.events2.get(enums_dto_1.WAHAEvents.POLL_VOTE).switch(pollVotes$);
        const messageAckWEBJS$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.MESSAGE_ACK, (message, ack) => {
            return { message, ack };
        });
        const messagesAckDM$ = messageAckWEBJS$.pipe((0, operators_1.map)((event) => event.message), (0, operators_1.map)(this.toWAMessage.bind(this)), (0, rxjs_1.filter)((ack) => !(0, jids_1.isJidGroup)(ack.to) && !(0, jids_1.isJidStatusBroadcast)(ack.to)), (0, rxjs_1.filter)((ack) => this.jids.include(ack.to)));
        const tagReceiptNode$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.TAG_RECEIPT);
        const messageAckGroups$ = tagReceiptNode$.pipe((0, rxjs_1.mergeMap)((node) => (0, ack_webjs_1.TagReceiptNodeToReceiptEvent)(node, this.getSessionMeInfo())), (0, rxjs_1.filter)(Boolean), (0, rxjs_1.mergeMap)(this.TagReceiptToMessageAck.bind(this)), (0, rxjs_1.filter)((ack) => (0, jids_1.isJidGroup)(ack.to) || (0, jids_1.isJidStatusBroadcast)(ack.to)), (0, rxjs_1.filter)((ack) => this.jids.include(ack.to)));
        const messageAckDMFinal$ = messagesAckDM$.pipe((0, reactive_1.DistinctAck)());
        const messageAckGroupsFinal$ = messageAckGroups$.pipe((0, reactive_1.DistinctAck)());
        this.events2.get(enums_dto_1.WAHAEvents.MESSAGE_ACK).switch(messageAckDMFinal$);
        this.events2
            .get(enums_dto_1.WAHAEvents.MESSAGE_ACK_GROUP)
            .switch(messageAckGroupsFinal$);
        const stateChanged$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.STATE_CHANGED);
        this.events2.get(enums_dto_1.WAHAEvents.STATE_CHANGE).switch(stateChanged$);
        const tagPresenceNode$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.TAG_PRESENCE);
        const presences$ = tagPresenceNode$.pipe((0, operators_1.map)(presence_1.TagPresenceToPresence), (0, rxjs_1.filter)(Boolean), (0, rxjs_1.filter)((presence) => this.jids.include(presence.id)));
        const tagChatstateNode$ = (0, rxjs_1.fromEvent)(this.whatsapp, 'tag:chatstate');
        const chatstatePresences$ = tagChatstateNode$.pipe((0, operators_1.map)(presence_1.TagChatstateToPresence), (0, rxjs_1.filter)(Boolean), (0, rxjs_1.filter)((presence) => this.jids.include(presence.id)));
        const presenceUpdate$ = (0, rxjs_1.merge)(presences$, chatstatePresences$);
        this.events2.get(enums_dto_1.WAHAEvents.PRESENCE_UPDATE).switch(presenceUpdate$);
        const groupJoin$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.GROUP_JOIN);
        this.events2.get(enums_dto_1.WAHAEvents.GROUP_JOIN).switch(groupJoin$);
        const groupV2Join$ = groupJoin$.pipe((0, rxjs_1.mergeMap)((evt) => (0, groups_webjs_1.ToGroupV2JoinEvent)(this.whatsapp, this.getSessionMeInfo().id, evt)), (0, rxjs_1.filter)(Boolean));
        this.events2.get(enums_dto_1.WAHAEvents.GROUP_V2_JOIN).switch(groupV2Join$);
        const groupLeave$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.GROUP_LEAVE);
        this.events2.get(enums_dto_1.WAHAEvents.GROUP_LEAVE).switch(groupLeave$);
        const groupV2Leave$ = groupLeave$.pipe((0, operators_1.map)((evt) => (0, groups_webjs_1.ToGroupV2LeaveEvent)(this.getSessionMeInfo().id, evt)), (0, rxjs_1.filter)(Boolean));
        this.events2.get(enums_dto_1.WAHAEvents.GROUP_V2_LEAVE).switch(groupV2Leave$);
        const groupAdminChanged$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.GROUP_ADMIN_CHANGED);
        const groupV2Participants = (0, rxjs_1.merge)(groupJoin$, groupLeave$, groupAdminChanged$).pipe((0, operators_1.map)(groups_webjs_1.ToGroupV2ParticipantsEvent), (0, rxjs_1.filter)(Boolean));
        this.events2
            .get(enums_dto_1.WAHAEvents.GROUP_V2_PARTICIPANTS)
            .switch(groupV2Participants);
        const groupUpdate$ = (0, rxjs_1.fromEvent)(this.whatsapp, whatsapp_web_js_1.Events.GROUP_UPDATE);
        const groupV2Update$ = groupUpdate$.pipe((0, rxjs_1.mergeMap)((evt) => (0, groups_webjs_1.ToGroupV2UpdateEvent)(this.whatsapp, evt)), (0, rxjs_1.filter)(Boolean));
        this.events2.get(enums_dto_1.WAHAEvents.GROUP_V2_UPDATE).switch(groupV2Update$);
        const chatArchived$ = (0, rxjs_1.fromEvent)(this.whatsapp, 'chat_archived', (chat, archived, _) => {
            return {
                chat: chat,
                archived: archived,
            };
        });
        const chatsArchived$ = chatArchived$.pipe((0, rxjs_1.filter)((event) => { var _a, _b; return this.jids.include((_b = (_a = event === null || event === void 0 ? void 0 : event.chat) === null || _a === void 0 ? void 0 : _a.id) === null || _b === void 0 ? void 0 : _b._serialized); }), (0, operators_1.map)((event) => {
            return {
                id: event.chat.id._serialized,
                archived: event.archived,
                timestamp: event.chat.timestamp,
            };
        }));
        this.events2.get(enums_dto_1.WAHAEvents.CHAT_ARCHIVE).switch(chatsArchived$);
        const call$ = (0, rxjs_1.fromEvent)(this.whatsapp, 'call');
        const calls$ = call$.pipe((0, rxjs_1.filter)((call) => this.jids.include(call === null || call === void 0 ? void 0 : call.from)), (0, operators_1.map)((call) => {
            return {
                id: call.id,
                from: call.from,
                timestamp: call.timestamp,
                isVideo: call.isVideo,
                isGroup: call.isGroup,
                _data: call,
            };
        }));
        this.events2.get(enums_dto_1.WAHAEvents.CALL_RECEIVED).switch(calls$);
        this.events2
            .get(enums_dto_1.WAHAEvents.CALL_REJECTED)
            .switch(this.callRejected$.asObservable());
    }
    async processIncomingMessage(message, downloadMedia = true) {
        var _a;
        const wamessage = this.toWAMessage(message);
        if (downloadMedia) {
            const media = await this.downloadMediaSafe(message);
            wamessage.media = media;
        }
        if (downloadMedia && ((_a = wamessage.replyTo) === null || _a === void 0 ? void 0 : _a.hasMedia)) {
            const quotedMessage = await message.getQuotedMessage().catch(() => null);
            if (quotedMessage) {
                wamessage.replyTo.media = await this.downloadMediaSafe(quotedMessage);
            }
        }
        return wamessage;
    }
    toRejectedCallData(peerJid, id) {
        const timestamp = Math.floor(Date.now() / 1000);
        return {
            id: id,
            from: peerJid,
            timestamp: timestamp,
            isVideo: false,
            isGroup: (0, jids_1.isJidGroup)(peerJid),
            _data: {
                id: id,
                from: peerJid,
                status: 'reject',
                api: true,
            },
        };
    }
    processMessageReaction(reaction) {
        if (this.lastQRDate) {
            if (reaction.timestamp < this.lastQRDate.getTime() / 1000) {
                return null;
            }
        }
        const twoDaysAgoSec = (Date.now() - WhatsappSessionWebJSCore.REACTION_MAX_AGE_MS) / 1000;
        if (reaction.timestamp < twoDaysAgoSec) {
            return null;
        }
        const source = this.getMessageSource(reaction.id.id);
        return {
            id: reaction.id._serialized,
            from: (0, jids_1.normalizeJid)(reaction.senderId),
            fromMe: reaction.id.fromMe,
            source: source,
            participant: reaction.senderId,
            to: reaction.id.remote,
            timestamp: reaction.timestamp,
            reaction: {
                text: reaction.reaction,
                messageId: reaction.msgId._serialized,
            },
        };
    }
    toPollVotePayload(vote) {
        var _a, _b, _c, _d, _e, _f, _g;
        const pollMessageId = (_b = (_a = vote === null || vote === void 0 ? void 0 : vote.parentMessage) === null || _a === void 0 ? void 0 : _a.id) === null || _b === void 0 ? void 0 : _b._serialized;
        if (!pollMessageId) {
            return null;
        }
        let pollKey;
        try {
            pollKey = (0, ids_1.parseMessageIdSerialized)(pollMessageId);
        }
        catch (error) {
            this.logger.warn({ pollMessageId, error }, 'Failed to parse poll message id for vote update');
            return null;
        }
        const chatId = (0, jids_1.toCusFormat)(((_d = (_c = vote === null || vote === void 0 ? void 0 : vote.parentMessage) === null || _c === void 0 ? void 0 : _c.id) === null || _d === void 0 ? void 0 : _d.remote) || pollKey.remoteJid);
        if (!this.jids.include(chatId)) {
            return null;
        }
        const meId = (_e = this.getSessionMeInfo()) === null || _e === void 0 ? void 0 : _e.id;
        const poll = (0, session_noweb_core_1.getDestination)(pollKey, meId);
        let voter = vote.voter;
        if (!voter) {
            return null;
        }
        voter = (0, jids_1.normalizeJid)(voter);
        const fromMe = !!meId && (0, jids_1.toCusFormat)(meId) === (0, jids_1.toCusFormat)(voter);
        const voteKey = {
            id: pollKey.id,
            remoteJid: pollKey.remoteJid,
            fromMe: fromMe,
            participant: (0, jids_1.isJidGroup)(chatId) ? voter : undefined,
        };
        const selectedOptions = (_g = (_f = vote === null || vote === void 0 ? void 0 : vote.selectedOptions) === null || _f === void 0 ? void 0 : _f.map((option) => option === null || option === void 0 ? void 0 : option.name).filter(Boolean)) !== null && _g !== void 0 ? _g : [];
        const pollVote = Object.assign(Object.assign({}, (0, session_noweb_core_1.getDestination)(voteKey, meId)), { selectedOptions: selectedOptions, timestamp: vote === null || vote === void 0 ? void 0 : vote.interractedAtTs });
        return {
            poll: poll,
            vote: pollVote,
            _data: vote,
        };
    }
    TagReceiptToMessageAck(receipt) {
        const ids = receipt.messageIds;
        const acks = [];
        for (const id_ of ids) {
            const messageKey = {
                fromMe: receipt.key.fromMe,
                remoteJid: (0, jids_1.toCusFormat)(receipt.key.remoteJid),
                participant: (0, jids_1.toCusFormat)(receipt.key.participant),
                id: id_,
            };
            const fromToParticipant = (0, session_noweb_core_1.getFromToParticipant)(messageKey);
            const id = (0, ids_1.SerializeMessageKey)(messageKey);
            const ack = (0, acks_1.StatusToAck)(receipt.status);
            acks.push({
                id: id,
                from: fromToParticipant.from,
                to: fromToParticipant.to,
                participant: (0, jids_1.toCusFormat)(receipt.participant),
                fromMe: !receipt.key.fromMe,
                ack: ack,
                ackName: enums_dto_1.WAMessageAck[ack] || enums_dto_1.ACK_UNKNOWN,
                _data: receipt._node,
            });
        }
        return acks;
    }
    toWAMessage(message) {
        var _a;
        const replyTo = this.extractReplyTo(message);
        const source = this.getMessageSource(message.id.id);
        // wwebjs leaves message.id._serialized undefined for group messages
        // whose participant is a LID ("@lid"); parseMessageIdSerialized
        // then throws "Cannot read properties of undefined (reading 'includes')"
        // and WAHA drops the event. Fall back to $1 (WWeb's raw serialized)
        // or rebuild from the id components. Remove when wwebjs > 1.34.7.
        const serialized = message.id._serialized
            || message.id.$1
            || (0, ids_1.SerializeMsgKey)(message.id);
        const key = (0, ids_1.parseMessageIdSerialized)(serialized);
        return {
            id: serialized,
            timestamp: message.timestamp,
            from: message.from,
            fromMe: message.fromMe,
            participant: (0, jids_1.toCusFormat)(key.participant),
            source: source,
            to: message.to,
            body: message.body,
            hasMedia: Boolean(message.hasMedia),
            media: null,
            mediaUrl: (_a = message.media) === null || _a === void 0 ? void 0 : _a.url,
            ack: message.ack,
            ackName: enums_dto_1.WAMessageAck[message.ack] || enums_dto_1.ACK_UNKNOWN,
            location: this.extractLocation(message),
            vCards: message.vCards,
            replyTo: replyTo,
            _data: message.rawData,
        };
    }
    extractReplyTo(message) {
        var _a;
        const rawData = message.rawData;
        const quotedMsg = rawData === null || rawData === void 0 ? void 0 : rawData.quotedMsg;
        const quotedStanzaId = (rawData === null || rawData === void 0 ? void 0 : rawData.quotedStanzaID) || (rawData === null || rawData === void 0 ? void 0 : rawData.quotedStanzaId);
        if (!quotedMsg && !quotedStanzaId) {
            return;
        }
        const quotedParticipant = (rawData === null || rawData === void 0 ? void 0 : rawData.quotedParticipant) || (quotedMsg === null || quotedMsg === void 0 ? void 0 : quotedMsg.author) || (quotedMsg === null || quotedMsg === void 0 ? void 0 : quotedMsg.from);
        return {
            id: quotedStanzaId || ((_a = quotedMsg === null || quotedMsg === void 0 ? void 0 : quotedMsg.id) === null || _a === void 0 ? void 0 : _a.id),
            participant: quotedParticipant,
            body: (quotedMsg === null || quotedMsg === void 0 ? void 0 : quotedMsg.caption) || (quotedMsg === null || quotedMsg === void 0 ? void 0 : quotedMsg.body),
            hasMedia: Boolean(quotedMsg === null || quotedMsg === void 0 ? void 0 : quotedMsg.directPath),
            media: null,
            _data: quotedMsg,
        };
    }
    extractLocation(message) {
        const location = message.location;
        if (lodash.isEmpty(location)) {
            return null;
        }
        const rawData = message.rawData;
        return {
            live: Boolean(rawData.isLive),
            latitude: location.latitude,
            longitude: location.longitude,
            name: location.name,
            address: location.address,
            description: rawData.comment || location.description,
            url: location.url,
            thumbnail: message.body,
        };
    }
    async getEngineInfo() {
        if (!this.whatsapp || !this.whatsapp.pupPage) {
            return null;
        }
        return {
            WWebVersion: await this.whatsapp.getWWebVersion(),
            state: await this.whatsapp.getState(),
        };
    }
    toWAContact(contact) {
        contact.id = contact.id._serialized;
        return contact;
    }
    async downloadMediaSafe(message) {
        try {
            return await this.downloadMedia(message);
        }
        catch (e) {
            this.logger.error('Failed when tried to download media for a message');
            this.logger.error(e, e.stack);
        }
        return null;
    }
    async downloadMedia(message) {
        let processor = new WEBJSEngineMediaProcessor();
        processor = new LottieMediaProcessorWrapper_1.LottieMediaProcessorWrapper(processor, this.logger);
        const media = await this.mediaManager.processMedia(processor, message, this.name);
        return media;
    }
    getMessageOptions(request) {
        let mentions = request.mentions;
        mentions = mentions ? mentions.map(this.ensureSuffix) : undefined;
        const quotedMessageId = request.reply_to || request.replyTo;
        return {
            mentions: mentions,
            quotedMessageId: quotedMessageId,
            linkPreview: request.linkPreview,
        };
    }
}
exports.WhatsappSessionWebJSCore = WhatsappSessionWebJSCore;
WhatsappSessionWebJSCore.REACTION_MAX_AGE_MS = 2 * 24 * 60 * 60 * 1000;
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.CheckNumberStatusQuery]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "checkNumberStatus", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "setProfileName", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "setProfileStatus", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "setProfilePicture", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "setGroupPicture", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "deleteGroupPicture", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "rejectCall", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessageTextRequest]),
    __metadata("design:returntype", void 0)
], WhatsappSessionWebJSCore.prototype, "sendText", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String]),
    __metadata("design:returntype", void 0)
], WhatsappSessionWebJSCore.prototype, "deleteMessage", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, chatting_dto_1.EditMessageRequest]),
    __metadata("design:returntype", void 0)
], WhatsappSessionWebJSCore.prototype, "editMessage", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessageContactVcardRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendContactVCard", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessageReplyRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "reply", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessagePollRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendPoll", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessageFileRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendFile", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessageImageRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendImage", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendVoice", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessageVideoRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendVideo", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessagePollVoteRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendPollVote", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessageButtonReply]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendButtonsReply", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessageLocationRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendLocation", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessageForwardRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "forwardMessage", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.SendSeenRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendSeen", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.ChatRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "startTyping", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.ChatRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "stopTyping", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessageReactionRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "setReaction", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [chatting_dto_1.MessageStarRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "setStar", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, chats_dto_1.ReadChatMessagesQuery]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "readChatMessages", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, Number]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "pinMessage", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "unpinMessage", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "deleteChat", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "clearMessages", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "chatsArchiveChat", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "chatsUnarchiveChat", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "chatsUnreadChat", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [labels_dto_1.LabelDTO]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "createLabel", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [labels_dto_1.Label]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "updateLabel", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [labels_dto_1.Label]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "deleteLabel", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", void 0)
], WhatsappSessionWebJSCore.prototype, "getChatsByLabelId", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Array]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "putLabelsToChat", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, contacts_dto_1.ContactUpdateBody]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "upsertContact", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "fetchContactProfilePicture", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [contacts_dto_1.ContactRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "blockContact", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [contacts_dto_1.ContactRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "unblockContact", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [groups_dto_1.CreateGroupRequest]),
    __metadata("design:returntype", void 0)
], WhatsappSessionWebJSCore.prototype, "createGroup", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", void 0)
], WhatsappSessionWebJSCore.prototype, "joinGroup", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", void 0)
], WhatsappSessionWebJSCore.prototype, "joinInfoGroup", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "setInfoAdminsOnly", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "setMessagesAdminsOnly", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "deleteGroup", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "leaveGroup", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "setDescription", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "setSubject", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "getInviteCode", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "revokeInviteCode", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, groups_dto_1.ParticipantsRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "addParticipants", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, groups_dto_1.ParticipantsRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "removeParticipants", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, groups_dto_1.ParticipantsRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "promoteParticipantsToAdmin", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, groups_dto_1.ParticipantsRequest]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "demoteParticipantsToUser", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [channels_dto_1.ChannelSearchByView]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "searchChannelsByView", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [channels_dto_1.ChannelSearchByText]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "searchChannelsByText", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, channels_dto_1.PreviewChannelMessages]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "previewChannelMessages", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [channels_dto_1.ListChannelsQuery]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "channelsList", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "channelsGetChannelByInviteCode", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "getPresence", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "subscribePresence", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [status_dto_1.ImageStatus]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendImageStatus", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [status_dto_1.VoiceStatus]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendVoiceStatus", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [status_dto_1.VideoStatus]),
    __metadata("design:returntype", Promise)
], WhatsappSessionWebJSCore.prototype, "sendVideoStatus", null);
__decorate([
    (0, activity_1.Activity)(),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [status_dto_1.TextStatus]),
    __metadata("design:returntype", void 0)
], WhatsappSessionWebJSCore.prototype, "sendTextStatus", null);
function NewsletterMetadataToChannel(data) {
    var _a, _b, _c;
    const pictureDirectPath = (_c = (_b = (_a = data.newsletterPictureMetadataMixin.picture) === null || _a === void 0 ? void 0 : _a[0].queryPictureDirectPathOrEmptyResponseMixinGroup) === null || _b === void 0 ? void 0 : _b.value) === null || _c === void 0 ? void 0 : _c.directPath;
    const pictureUrl = (0, session_abc_1.getPublicUrlFromDirectPath)(pictureDirectPath);
    return {
        id: data.idJid,
        name: data.newsletterNameMetadataMixin.nameElementValue,
        picture: pictureUrl,
        description: data.newsletterDescriptionMetadataMixin
            .descriptionQueryDescriptionResponseMixin.elementValue,
        invite: (0, session_abc_1.getChannelInviteLink)(data.newsletterInviteLinkMetadataMixin.inviteCode),
        subscribersCount: Number(data.newsletterSubscribersMetadataMixin.subscribersCount),
        verified: data.newsletterVerificationMetadataMixin.verificationState === 'verified',
    };
}
class WEBJSEngineMediaProcessor {
    hasMedia(message) {
        if (!message.hasMedia) {
            return false;
        }
        return message.type !== 'revoked';
    }
    getChatId(message) {
        return message.id.remote;
    }
    getMessageId(message) {
        return message.id._serialized;
    }
    getMimetype(message) {
        return message.rawData.mimetype;
    }
    async getMediaBuffer(message) {
        return message.downloadMedia().then((media) => {
            if (!media) {
                return null;
            }
            return Buffer.from(media.data, 'base64');
        });
    }
    getFilename(message) {
        var _a;
        return ((_a = message.rawData) === null || _a === void 0 ? void 0 : _a.filename) || null;
    }
}
exports.WEBJSEngineMediaProcessor = WEBJSEngineMediaProcessor;
//# sourceMappingURL=session.webjs.core.js.map