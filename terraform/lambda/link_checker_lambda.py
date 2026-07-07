# -*- coding: utf-8 -*-
# 必要なライブラリをインポート
import os
import json
import urllib.parse
import logging
import time
import re
import boto3
import requests
import csv
import io
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- グローバル設定 ---
logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))
s3_client = boto3.client('s3')
JST = timezone(timedelta(hours=+9), 'JST')

# --- 定数定義 ---
MAX_META_REFRESH_REDIRECTS = 5
HTTP_RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
PER_ARTICLE_WAIT_SECONDS = 1
SUCCESS_STATUS_LOWER_BOUND = 200
SUCCESS_STATUS_UPPER_BOUND = 400

# ★★★【変更】ご指定のCSVヘッダー形式に修正 ★★★
CSV_HEADERS = [
    "スプレッドシート記載のリンク", "ブログ記事URL", "アフィリエイト広告リンク",
    "確認結果", "ステータスコード", "アフィリエイト広告リンク先URL", "エラーメッセージ", "タイムスタンプ"
]

# GAS側 (post_result_s3download.gs の checkLambdaCompletionStatus_) が参照するフラグファイル
COMPLETION_STATUS_FILE_KEY = "lambda_completion_status.json"

# --- 環境変数からの設定読み込み ---
try:
    S3_OUTPUT_BUCKET = os.environ['S3_OUTPUT_BUCKET']
    REQUEST_TIMEOUT = int(os.environ['REQUEST_TIMEOUT'])
    MAX_RETRIES = int(os.environ['MAX_RETRIES'])
    BACKOFF_FACTOR = float(os.environ['BACKOFF_FACTOR'])
    MAX_WORKERS = int(os.environ['MAX_WORKERS'])
    CRAWL_WAIT_SECONDS = int(os.environ['CRAWL_WAIT_SECONDS'])
    if not S3_OUTPUT_BUCKET:
        raise ValueError("S3_OUTPUT_BUCKET is set but empty.")
except (KeyError, ValueError, TypeError) as e:
    logger.error(f"必須の環境変数が設定されていないか、値が不正です。エラー: {e}")
    raise

# --- 補助関数 (変更なし) ---

def requests_retry_session(retries=MAX_RETRIES, backoff_factor=BACKOFF_FACTOR, status_forcelist=HTTP_RETRY_STATUS_CODES, session=None):
    session = session or requests.Session()
    retry = Retry(
        total=retries, read=retries, connect=retries,
        backoff_factor=backoff_factor, status_forcelist=status_forcelist,
        respect_retry_after_header=True
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def get_html_content(url):
    try:
        session = requests_retry_session()
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = session.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
        response.raise_for_status()
        response.encoding = response.apparent_encoding
        return response.text
    except requests.exceptions.RequestException as e:
        logger.error(f"URL取得エラー {url}: {e}")
        return None

def extract_ad_links(html_content, base_url):
    if not html_content: return None
    soup = BeautifulSoup(html_content, 'html.parser')
    body = soup.body
    if not body: return None
    ad_notice_texts = body.find_all(string=re.compile(r"※一部、広告・宣伝が含まれます。"))
    if not ad_notice_texts: return None
    links = set()
    for notice_text in ad_notice_texts:
        for next_element in notice_text.find_all_next():
            if next_element.name == 'a' and next_element.has_attr('href'):
                href = next_element['href']
                if href and not href.lower().startswith('javascript:'):
                    full_url = urllib.parse.urljoin(base_url, href)
                    if full_url.split('#')[0] != base_url.split('#')[0]:
                        links.add(full_url)
                    break
    return [link for link in links if not urllib.parse.urlparse(link).fragment]

def find_hatena_next_page_link(html_content, base_url):
    if not html_content: return None
    soup = BeautifulSoup(html_content, 'html.parser')
    next_link_tag = soup.find('a', rel='next', href=True)
    if next_link_tag:
        return urllib.parse.urljoin(base_url, next_link_tag['href'])
    return None

def extract_livedoor_article_links(html_content, base_url):
    links = set()
    if not html_content: return list(links)
    soup = BeautifulSoup(html_content, 'html.parser')
    for article in soup.find_all('article', class_=re.compile(r'article')):
        title_link = article.select_one('h1.article-title a, h2.article-title a, a.article-title-link')
        if title_link and title_link.has_attr('href'):
            href = title_link['href']
            if href and not href.startswith('#') and not href.lower().startswith('javascript:'):
                full_url = urllib.parse.urljoin(base_url, href)
                links.add(full_url.split('#')[0])
    return list(links)

def find_livedoor_next_page_link(html_content, base_url):
    if not html_content: return None
    soup = BeautifulSoup(html_content, 'html.parser')
    next_link_tag = soup.select_one('a.next, a.pager-next, a:-soup-contains("»"), a:-soup-contains("次へ")')
    if next_link_tag and next_link_tag.has_attr('href'):
        return urllib.parse.urljoin(base_url, next_link_tag['href'])
    return None

def check_link_status(url, ng_words=None):
    session = requests_retry_session()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36', 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8', 'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8'}
    current_url = url
    for _ in range(MAX_META_REFRESH_REDIRECTS):
        try:
            response = session.get(current_url, timeout=REQUEST_TIMEOUT, headers=headers, allow_redirects=True)
            response.raise_for_status()
            response.encoding = response.apparent_encoding
            page_content = response.text
            soup = BeautifulSoup(page_content, 'html.parser')
            refresh_tag = soup.find('meta', attrs={'http-equiv': re.compile(r'refresh', re.I)})
            if refresh_tag and refresh_tag.get('content'):
                content_attr = refresh_tag['content'].lower()
                match = re.search(r'url=(.+)', content_attr)
                if match:
                    next_url = match.group(1).strip().strip("'\"")
                    current_url = urllib.parse.urljoin(response.url, next_url)
                    continue
            if ng_words:
                for word in ng_words:
                    if word in page_content:
                        return {"status_code": response.status_code, "final_url": response.url, "error_message": f"ページ内にNGワードが含まれています: '{word}'"}
            return {"status_code": response.status_code, "final_url": response.url, "error_message": None}
        except requests.exceptions.HTTPError as e:
            return {"status_code": e.response.status_code if e.response else None, "final_url": e.response.url if e.response else current_url, "error_message": str(e)}
        except requests.exceptions.RequestException as e:
            return {"status_code": None, "final_url": current_url, "error_message": str(e)}
    return {"status_code": None, "final_url": current_url, "error_message": "Meta refresh redirect limit exceeded"}

def write_completion_status(status, error_message=None):
    """GAS側の後続処理(mainPostProcess)が本日分の完了可否を判定するためのフラグファイルをS3に書き込みます。"""
    try:
        body = {
            "status": status,
            "last_success_date": datetime.now(JST).strftime('%Y-%m-%d'),
            "timestamp": datetime.now(JST).isoformat()
        }
        if error_message:
            body["error_message"] = error_message
        s3_client.put_object(
            Bucket=S3_OUTPUT_BUCKET,
            Key=COMPLETION_STATUS_FILE_KEY,
            Body=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            ContentType='application/json'
        )
        logger.info(f"完了ステータスファイルを s3://{S3_OUTPUT_BUCKET}/{COMPLETION_STATUS_FILE_KEY} にアップロードしました (status={status})")
    except Exception as e:
        logger.error(f"完了ステータスファイルのアップロードに失敗しました: {e}")

# --- メイン処理 (Lambdaハンドラ) ---

def lambda_handler(event, context):
    try:
        logger.info(f"イベント受信: {json.dumps(event)}")
        
        ng_words_str = os.environ.get('NG_WORDS', '')
        ng_words = [word.strip() for word in ng_words_str.split(',') if word.strip()]
        exclude_strings_str = os.environ.get('EXCLUDE_STRINGS', '')
        exclude_strings = [s.strip() for s in exclude_strings_str.split(',') if s.strip()]
        if exclude_strings:
            logger.info(f"チェック対象から除外する文字列: {exclude_strings}")

        if 'Records' not in event or not event['Records']:
            return {'statusCode': 400, 'body': json.dumps({'message': 'S3レコードがイベントに見つかりません。'}, ensure_ascii=False)}
        
        s3_record = event['Records'][0]['s3']
        input_bucket_name = s3_record['bucket']['name']
        input_object_key = urllib.parse.unquote_plus(s3_record['object']['key'], encoding='utf-8')
        response = s3_client.get_object(Bucket=input_bucket_name, Key=input_object_key)
        input_data = json.loads(response['Body'].read().decode('utf-8'))
        
        auto_urls = input_data.get('auto_url_list', [])
        manual_urls = input_data.get('manual_url_list', [])
        all_results_for_csv = []

        # ★★★【変更】結果を判定し、新しいCSV形式の辞書を返す共通関数 ★★★
        def process_check_result(check_result, original_item):
            status_code = check_result.get("status_code")
            final_url = check_result.get("final_url")
            error_message = check_result.get("error_message")
            confirmation_result = "OK"  # デフォルトをOKとする
            
            is_successful_status = status_code and SUCCESS_STATUS_LOWER_BOUND <= status_code < SUCCESS_STATUS_UPPER_BOUND
            
            if not is_successful_status or error_message:
                confirmation_result = "NG"
                # エラーメッセージがない場合はステータスコードを理由とする
                if not error_message:
                    error_message = f"ステータスコード異常: {status_code}"
            else: # ステータスが正常な場合でも追加のドメインチェック
                parsed_final_url = urllib.parse.urlparse(final_url)
                blog_url_str = original_item.get('spreadsheet_link') or original_item.get('url') or ''
                parsed_blog_url = urllib.parse.urlparse(blog_url_str)
                if parsed_final_url.netloc == "jass-net.com":
                    confirmation_result, error_message = "NG", "リンク先のドメインが 'jass-net.com' です"
                elif "hatena" in final_url and parsed_blog_url and parsed_final_url.netloc != parsed_blog_url.netloc:
                    confirmation_result, error_message = "NG", "リンク先のURLに 'hatena' が含まれています"

            return {
                "スプレッドシート記載のリンク": original_item.get("spreadsheet_link") or original_item.get("url"),
                "ブログ記事URL": original_item.get("blog_article_url"),
                "アフィリエイト広告リンク": original_item.get("affiliate_link"),
                "確認結果": confirmation_result,
                "ステータスコード": status_code,
                "アフィリエイト広告リンク先URL": final_url,
                "エラーメッセージ": error_message,
                "タイムスタンプ": datetime.now(JST).isoformat()
            }

        # --- 自動URLリストの処理 ---
        logger.info(f"自動URLリストの処理を開始します。件数: {len(auto_urls)}")
        for target_item in auto_urls:
            blog_url = target_item.get('url')
            if not blog_url: continue
            
            is_hatena = "hatenablog.com" in blog_url or "hatenablog.jp" in blog_url
            is_livedoor = "livedoor.blog" in blog_url or "blog.jp" in blog_url
            
            if is_hatena:
                current_page_url = blog_url
                while current_page_url:
                    html_content = get_html_content(current_page_url)
                    if not html_content: break
                    extracted_links = extract_ad_links(html_content, current_page_url)
                    if extracted_links is not None:
                        filtered_links = [link for link in extracted_links if not any(ex_str in link for ex_str in exclude_strings)]
                        if not filtered_links:
                            all_results_for_csv.append({"スプレッドシート記載のリンク": blog_url, "ブログ記事URL": current_page_url, "アフィリエイト広告リンク": current_page_url, "確認結果": "NG", "ステータスコード": None, "アフィリエイト広告リンク先URL": current_page_url, "エラーメッセージ": "対象の広告リンクが見つかりませんでした", "タイムスタンプ": datetime.now(JST).isoformat()})
                        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                            future_to_link = {executor.submit(check_link_status, link, ng_words): link for link in filtered_links}
                            for future in as_completed(future_to_link):
                                link = future_to_link[future]
                                try:
                                    check_result = future.result()
                                    original_item_data = {"url": blog_url, "blog_article_url": current_page_url, "affiliate_link": link}
                                    processed_result = process_check_result(check_result, original_item_data)
                                    all_results_for_csv.append(processed_result)
                                except Exception as exc:
                                    logger.error(f"リンクチェック中に例外が発生しました {link}: {exc}")
                                    all_results_for_csv.append({"スプレッドシート記載のリンク": blog_url, "ブログ記事URL": current_page_url, "アフィリエイト広告リンク": link, "確認結果": "NG", "ステータスコード": None, "アフィリエイト広告リンク先URL": link, "エラーメッセージ": str(exc), "タイムスタンプ": datetime.now(JST).isoformat()})
                    current_page_url = find_hatena_next_page_link(html_content, current_page_url)
                    if current_page_url: time.sleep(CRAWL_WAIT_SECONDS)
            elif is_livedoor:
                all_article_urls = set()
                current_list_page_url = blog_url
                while current_list_page_url:
                    list_page_html = get_html_content(current_list_page_url)
                    if not list_page_html: break
                    all_article_urls.update(extract_livedoor_article_links(list_page_html, current_list_page_url))
                    current_list_page_url = find_livedoor_next_page_link(list_page_html, current_list_page_url)
                    if current_list_page_url: time.sleep(CRAWL_WAIT_SECONDS)
                for article_url in all_article_urls:
                    time.sleep(PER_ARTICLE_WAIT_SECONDS)
                    article_html = get_html_content(article_url)
                    if not article_html: continue
                    extracted_links = extract_ad_links(article_html, article_url)
                    if extracted_links is not None:
                        filtered_links = [link for link in extracted_links if not any(ex_str in link for ex_str in exclude_strings)]
                        if not filtered_links:
                            all_results_for_csv.append({"スプレッドシート記載のリンク": blog_url, "ブログ記事URL": article_url, "アフィリエイト広告リンク": article_url, "確認結果": "NG", "ステータスコード": None, "アフィリエイト広告リンク先URL": article_url, "エラーメッセージ": "対象の広告リンクが見つかりませんでした", "タイムスタンプ": datetime.now(JST).isoformat()})
                        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                            future_to_link = {executor.submit(check_link_status, link, ng_words): link for link in filtered_links}
                            for future in as_completed(future_to_link):
                                link = future_to_link[future]
                                try:
                                    check_result = future.result()
                                    original_item_data = {"url": blog_url, "blog_article_url": article_url, "affiliate_link": link}
                                    processed_result = process_check_result(check_result, original_item_data)
                                    all_results_for_csv.append(processed_result)
                                except Exception as exc:
                                    logger.error(f"リンクチェック中に例外が発生しました {link}: {exc}")
                                    all_results_for_csv.append({"スプレッドシート記載のリンク": blog_url, "ブログ記事URL": article_url, "アフィリエイト広告リンク": link, "確認結果": "NG", "ステータスコード": None, "アフィリエイト広告リンク先URL": link, "エラーメッセージ": str(exc), "タイムスタンプ": datetime.now(JST).isoformat()})
            else:
                logger.warning(f"サポート外のブログタイプです: {blog_url}")

        # --- 手動URLリストの処理 ---
        logger.info(f"手動URLリストの処理を開始します。件数: {len(manual_urls)}")
        filtered_manual_urls = [item for item in manual_urls if item.get('affiliate_link') and not any(ex_str in item.get('affiliate_link') for ex_str in exclude_strings)]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_manual_item = {executor.submit(check_link_status, item.get('affiliate_link'), ng_words): item for item in filtered_manual_urls}
            for future in as_completed(future_to_manual_item):
                manual_item = future_to_manual_item[future]
                try:
                    check_result = future.result()
                    processed_result = process_check_result(check_result, manual_item)
                    all_results_for_csv.append(processed_result)
                except Exception as exc:
                    logger.error(f"手動リンクチェック中に例外が発生しました {manual_item.get('affiliate_link')}: {exc}")
                    all_results_for_csv.append({"スプレッドシート記載のリンク": manual_item.get('spreadsheet_link'), "ブログ記事URL": manual_item.get('blog_article_url'), "アフィリエイト広告リンク": manual_item.get('affiliate_link'), "確認結果": "NG", "ステータスコード": None, "アフィリエイト広告リンク先URL": manual_item.get('affiliate_link'), "エラーメッセージ": str(exc), "タイムスタンプ": datetime.now(JST).isoformat()})
        
        # --- 結果の出力 ---
        logger.info(f"CSV出力対象の結果件数: {len(all_results_for_csv)}")
        if S3_OUTPUT_BUCKET:
            csv_output_key = "linkcheck_result.csv"
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=CSV_HEADERS)
            writer.writeheader()
            if all_results_for_csv:
                all_results_for_csv.sort(key=lambda x: (str(x.get('スプレッドシート記載のリンク', '')), str(x.get('ブログ記事URL', '')), str(x.get('アフィリエイト広告リンク', ''))))
                writer.writerows(all_results_for_csv)
            csv_body = output.getvalue()
            s3_client.put_object(Bucket=S3_OUTPUT_BUCKET, Key=csv_output_key, Body=csv_body.encode('utf-8-sig'), ContentType='text/csv')
            logger.info(f"結果CSVを s3://{S3_OUTPUT_BUCKET}/{csv_output_key} にアップロードしました")
            write_completion_status('SUCCESS')
        else:
            logger.error("S3_OUTPUT_BUCKET 環境変数が設定されていません。結果をアップロードできません。")

        return {'statusCode': 200, 'body': json.dumps({'message': 'リンクチェック処理が正常に完了しました！'}, ensure_ascii=False)}

    except Exception as e:
        logger.error(f"リンクチェック処理中に予期せぬエラーが発生しました: {e}", exc_info=True)
        write_completion_status('FAILURE', error_message=str(e))
        return {'statusCode': 500, 'body': json.dumps({'message': f'リンクチェック処理中にエラーが発生しました: {str(e)}'}, ensure_ascii=False)}