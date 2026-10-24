#!/usr/bin/env python3
"""Deliberate local enhancements layered on top of the faithful conversion.

These tables were carried over verbatim from the previous converter. They are
*enhancements*, not part of the upstream Loon plugins: they must never preempt
an upstream rewrite/script for the same URL (see UPSTREAM_CONFLICT_PATCHES in
kelee_config.py) and every merged rule is tagged in the build manifest.

The QUIC mirror/PDD tables are documented in README and can be disabled by the
build orchestrator when publishing a strictly faithful bundle.
"""
from __future__ import annotations

# Some iOS apps bypass the system HTTP proxy and open TCP connections directly
# through the tunnel. Keep force-http-engine narrowly scoped to the hosts whose
# Loon plugin explicitly expects MitM/rewrite processing.
FORCE_HTTP_ENGINE_OVERRIDES = {
    "PinDuoDuo_remove_ads.lpx": [
        "api.pinduoduo.com:443",
        "m.pinduoduo.net:443",
    ],
}

# Extra blocking surface for apps whose upstream Loon plugin only covers a
# subset of the ad/tracking endpoints found in community rule sets (anti-AD,
# Cats-Team, ClydeTime). Merged into every converted override so the output
# stays self-contained even when the Cats-Team rule set is absent.
APP_EXTRA_RULES = {
    "Himalaya_remove_ads.lpx": [
        "DOMAIN-SUFFIX,ad.ximalaya.com,REJECT",
        "DOMAIN-SUFFIX,adsebs.ximalaya.com,REJECT",
        "DOMAIN-SUFFIX,adbs.ximalaya.com,REJECT",
        "DOMAIN-SUFFIX,adwbs.ximalaya.com,REJECT",
        "DOMAIN-SUFFIX,dns.ximalaya.com,REJECT",
        "DOMAIN-SUFFIX,linkeye.ximalaya.com,REJECT",
        "DOMAIN-SUFFIX,location.ximalaya.com,REJECT",
        "DOMAIN-SUFFIX,xdcs-collector.ximalaya.com,REJECT",
        "DOMAIN-SUFFIX,xdcs-collectorhera.ximalaya.com,REJECT",
    ],
    # Ad-serving hosts from anti-AD that the upstream plugin does not reject.
    "Amap_remove_ads.lpx": [
        "DOMAIN-SUFFIX,optimus-ads.amap.com,REJECT",
        "DOMAIN-SUFFIX,adashx.ut.amap.com,REJECT",
        "DOMAIN-SUFFIX,h-adashx.ut.amap.com,REJECT",
        "DOMAIN-SUFFIX,v6-adashx.ut.amap.com,REJECT",
        # RuCu6/ClydeTime/fmz200 cross-check (2026-08)
        "DOMAIN-SUFFIX,awaken.amap.com,REJECT",
        "DOMAIN-SUFFIX,amdc.m.taobao.com,REJECT",
        "DOMAIN-SUFFIX,adiu.amap.com,REJECT",
        "DOMAIN-SUFFIX,optimus-ads.amap.com.w.alikunlun.com,REJECT",
        "DOMAIN-SUFFIX,logs.amap.com,REJECT",
        "DOMAIN-SUFFIX,dualstack-logs.amap.com,REJECT",
        "DOMAIN-SUFFIX,page.amap.com,REJECT",
    ],
    "JD_remove_ads.lpx": [
        "DOMAIN-SUFFIX,ad.3.cn,REJECT",
        "DOMAIN-SUFFIX,addata.jd.com,REJECT",
        "DOMAIN-SUFFIX,ads-union.jd.com,REJECT",
        "DOMAIN-SUFFIX,ads.union.jd.com,REJECT",
        "DOMAIN-SUFFIX,dsp-x.jd.com,REJECT",
        "DOMAIN-SUFFIX,jrad.jd.com,REJECT",
        # fmz200 JD.com snippet cross-check (2026-08)
        "DOMAIN-SUFFIX,jzt.jd.com,REJECT",
        "DOMAIN-SUFFIX,bdsp.x.jd.com,REJECT",
        "DOMAIN-SUFFIX,kepler.jd.com,REJECT",
        "DOMAIN-SUFFIX,keplerapi.jd.com,REJECT",
        "DOMAIN-SUFFIX,c-nfa.jd.com,REJECT",
        "DOMAIN-SUFFIX,jd.c-ptsp.com.cn,REJECT",
        "DOMAIN-SUFFIX,cm.jd.com,REJECT",
        "DOMAIN-SUFFIX,policy.jd.com,REJECT",
        "DOMAIN-SUFFIX,du.jd.com,REJECT",
        "DOMAIN-SUFFIX,jddebug.com,REJECT",
        "DOMAIN-SUFFIX,mapi.m.jd.com,REJECT",
        "DOMAIN-SUFFIX,dns.jd.com,REJECT",
    ],
    "Taobao_remove_ads.lpx": [
        "DOMAIN-SUFFIX,afpssp.alimama.com,REJECT",
        "DOMAIN-SUFFIX,afptrack.alimama.com,REJECT",
        "DOMAIN-SUFFIX,banner.alimama.com,REJECT",
        "DOMAIN-SUFFIX,cms.opendsp.tanx.com,REJECT",
        "DOMAIN-SUFFIX,ef.opendsp.tanx.com,REJECT",
        "DOMAIN-SUFFIX,dsp.simba.taobao.com,REJECT",
        "DOMAIN-SUFFIX,rtb.julang.taobao.com,REJECT",
        "DOMAIN-SUFFIX,sdk-config.tanx.com,REJECT",
        "DOMAIN-SUFFIX,sdkinit.taobao.com,REJECT",
        "DOMAIN-SUFFIX,taobaoad.com,REJECT",
        "DOMAIN-SUFFIX,tunion-api.m.taobao.com,REJECT",
        # fmz200 Taobao snippet cross-check (2026-08): 直通车/百川/追踪
        "DOMAIN-SUFFIX,fav.simba.taobao.com,REJECT",
        "DOMAIN-SUFFIX,m.simba.taobao.com,REJECT",
        "DOMAIN-SUFFIX,mclick.simba.taobao.com,REJECT",
        "DOMAIN-SUFFIX,redirect.simba.taobao.com,REJECT",
        "DOMAIN-SUFFIX,srd.simba.taobao.com,REJECT",
        "DOMAIN-SUFFIX,textlink.simba.taobao.com,REJECT",
        "DOMAIN-SUFFIX,tns.simba.taobao.com,REJECT",
        "DOMAIN-SUFFIX,click.simba.taobao.com,REJECT",
        "DOMAIN-SUFFIX,click.mz.simba.taobao.com,REJECT",
        "DOMAIN-SUFFIX,click.tz.simba.taobao.com,REJECT",
        "DOMAIN-SUFFIX,stat.simba.taobao.com,REJECT",
        "DOMAIN-SUFFIX,simba.m.taobao.com,REJECT",
        "DOMAIN-SUFFIX,temai.taobao.com,REJECT",
        "DOMAIN-SUFFIX,unitacs.m.taobao.com,REJECT",
        "DOMAIN-SUFFIX,acs4baichuan.m.taobao.com,REJECT",
        "DOMAIN-SUFFIX,h-adashx.ut.taobao.com,REJECT",
        "DOMAIN-SUFFIX,adashbc.ut.taobao.com,REJECT",
        "DOMAIN-SUFFIX,adash-c.ut.taobao.com,REJECT",
        "DOMAIN-SUFFIX,adashxgc.ut.taobao.com,REJECT",
        "DOMAIN-SUFFIX,adashx4yt.m.taobao.com,REJECT",
        "DOMAIN-SUFFIX,m-adash.m.taobao.com,REJECT",
        "DOMAIN-SUFFIX,adashbc.m.taobao.com,REJECT",
    ],
    "Weixin_Official_Accounts_remove_ads.lpx": [
        "DOMAIN-SUFFIX,ad.weixin.qq.com,REJECT",
    ],
    "DragonRead_remove_ads.lpx": [
        "DOMAIN-SUFFIX,log3-applog-hl.fqnovel.com,REJECT",
        "DOMAIN-SUFFIX,log5-applog-hl.fqnovel.com,REJECT",
        "DOMAIN-SUFFIX,rtlog3-applog-hl.fqnovel.com,REJECT",
        "DOMAIN-SUFFIX,rtlog5-applog-hl.fqnovel.com,REJECT",
        # fmz200 FanQieNovel snippet cross-check (2026-08)
        "DOMAIN-SUFFIX,pangolin-sdk-toutiao.com,REJECT",
        "DOMAIN-SUFFIX,byteorge.com,REJECT",
        "DOMAIN-SUFFIX,api.iegadp.qq.com,REJECT",
        "DOMAIN-SUFFIX,novelapp.ixigua.com,REJECT",
        "DOMAIN-SUFFIX,ug-sinfonlinea.bytedance.com,REJECT",
        "DOMAIN-SUFFIX,activity-ag.awemeughun.com,REJECT",
        "DOMAIN-SUFFIX,tnc3-alisc1.snssdk.com,REJECT",
        "DOMAIN-SUFFIX,tnc3-aliec2.snssdk.com,REJECT",
        "DOMAIN-SUFFIX,security-lq.snssdk.com,REJECT",
    ],
    "Zhihu_remove_ads.lpx": [
        # fmz200 Zhihu snippet cross-check (2026-08); mqtt 保留以维持通知推送
        "DOMAIN-SUFFIX,appcloud.zhihu.com,REJECT",
        "DOMAIN-SUFFIX,appcloud2.in.zhihu.com,REJECT",
        "DOMAIN-SUFFIX,sugar.zhihu.com,REJECT",
        "DOMAIN-SUFFIX,crash2.zhihu.com,REJECT",
    ],
}

APP_EXTRA_MITM = {
    "Himalaya_remove_ads.lpx": [
        "api.ximalaya.com",
        "apihera.ximalaya.com",
        "apiwsa.ximalaya.com",
    ],
    "Bilibili_remove_ads.lpx": [
        "api.vc.bilibili.com",
    ],
    "Zhihu_remove_ads.lpx": [
        "zhuanlan.zhihu.com",
    ],
}

APP_EXTRA_REWRITES = {
    "Himalaya_remove_ads.lpx": [
        r"^https?:\/\/.*\.ximalaya\.com\/api\/v\d\/adRealTime reject-dict",
        r"^https?:\/\/.*\.ximalaya\.com\/ting\/(loading|feed|home) reject-dict",
        r"^https?:\/\/.*\.ximalaya\.com\/discovery-feed\/focus\/queryF reject-dict",
    ],
    "Amap_remove_ads.lpx": [
        r"^https?:\/\/mps\.amap\.com\/ws\/mps\/scene\? reject-dict",
        r"^https?:\/\/m5\.amap\.com\/ws\/shield\/dsp\/app\/startup\/init\? reject-dict",
        r"^https?:\/\/m5\.amap\.com\/ws\/shield\/search_poi\/city_feed reject-dict",
        r"^https?:\/\/m5\.amap\.com\/ws\/asa\/ads_attribution\? reject-dict",
        r"^https?:\/\/sns\.amap\.com\/ws\/msgbox\/pull(?:_mp)? reject-dict",
        r"^https?:\/\/sns\.amap\.com\/ws\/userview\/footprint reject-dict",
    ],
    "JD_remove_ads.lpx": [
        r"^https:\/\/api\.m\.jd\.com\/client\.action\?functionId=getWidgetV1052 reject-dict",
    ],
    "Taobao_remove_ads.lpx": [
        r"^https?:\/\/poplayer\.template\.alibaba\.com\/\w+\.json reject-dict",
    ],
    "FleaMarket_remove_ads.lpx": [
        r"^https:\/\/acs\.m\.goofish\.com\/gw\/mtop\.taobao\.idlemtopsearch\.(item\.search\.activate|search\.discover)\/ reject-dict",
        r"^https:\/\/acs\.m\.goofish\.com\/gw\/mtop\.taobao\.idle\.item\.resell\.recommendorhotcate\/1\.0 reject-dict",
        r"^https:\/\/acs\.m\.goofish\.com\/gw\/mtop\.taobao\.idlehome\.idle\.coin\.nextfresh\/1\.0 reject-dict",
        r"^https:\/\/acs\.m\.goofish\.com\/gw\/mtop\.taobao\.idle\.fun\.follow\.often\.visit reject-dict",
        r"^https:\/\/acs\.m\.goofish\.com\/gw\/mtop\.taobao\.idle\.(group|circle)\.myself\.banner\/ reject-dict",
        r"^https:\/\/acs\.m\.goofish\.com\/gw\/mtop\.taobao\.idle\.circle\.visited\/1\.0 reject-dict",
    ],
    "Zhihu_remove_ads.lpx": [
        r"^https:\/\/api\.zhihu\.com\/ad-style-service\/request reject-dict",
        r"^https:\/\/api\.zhihu\.com\/bazaar\/float_window reject-dict",
        r"^https:\/\/api\.zhihu\.com\/market\/popovers_v2 reject-dict",
        r"^https:\/\/api\.zhihu\.com\/me\/guides reject-dict",
        r"^https:\/\/api\.zhihu\.com\/comment_v5\/(articles|answers)\/\d+\/list-headers reject-dict",
        r"^https:\/\/api\.zhihu\.com\/content-distribution-core\/bubble\/common\/settings reject-dict",
        r"^https:\/\/api\.zhihu\.com\/(moments\/lastread|drama\/hot-drama-list) reject-dict",
        r"^https:\/\/api\.zhihu\.com\/root\/window reject-dict",
        r"^https:\/\/api\.zhihu\.com\/ab\/api\/v1\/products\/zhihu\/platforms\/ios\/config reject-dict",
        r"^https:\/\/api\.zhihu\.com\/distribute\/rhea\/qa_ad_card\/h5\/recommendation\? reject-dict",
        r"^https:\/\/api\.zhihu\.com\/prague\/related_suggestion_native\/feed\? reject-dict",
        r"^https:\/\/api\.zhihu\.com\/v5\.1\/topics\/answer\/\d+\/relation reject-dict",
        r"^https:\/\/www\.zhihu\.com\/api\/v4\/hot_recommendation reject-dict",
        r"^https:\/\/www\.zhihu\.com\/api\/v4\/mcn\/v2\/linkcards\? reject-dict",
        r"^https:\/\/www\.zhihu\.com\/api\/v4\/(answers|questions)\/\d+\/related-readings reject-dict",
        r"^https:\/\/www\.zhihu\.com\/api\/v4\/search\/related_queries\/(article|answer)\/\d+ reject-dict",
        r"^https:\/\/zhuanlan\.zhihu\.com\/api\/articles\/\d+\/recommendation reject-dict",
    ],
    "Bilibili_remove_ads.lpx": [
        r"^https:\/\/api\.vc\.bilibili\.com\/(search_svr\/v\d\/Search\/recommend_words|topic_svr\/v1\/topic_svr|dynamic_svr\/v1\/dynamic_svr\/mix_uplist) reject-dict",
        r"^https:\/\/api\.bili(bili\.com|api\.net)\/pgc\/season\/app\/related\/recommend\? reject-dict",
    ],
}

# QUIC mirroring is skipped for the shared advertiser-blocking dependency:
# hundreds of third-party SDK domains there are TCP-dominant, and mirroring
# them would roughly double the override size for little benefit.
QUIC_MIRROR_EXCLUDE = {"BlockAdvertisers.lpx"}
