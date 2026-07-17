/**
 * @fileoverview 事後処理を担当するスクリプトです。
 * S3上のリンクチェック結果(CSV)をスプレッドシートに反映し、差分をメール通知します。
 */

// =============================================================================
// --- グローバル設定 ---
// =============================================================================
// ※ CONFIGオブジェクトや定数は、実際の環境に合わせて設定してください。
// const CONFIG = { ... };
// const SHEET_NAME_TODAY = '...';
// const SHEET_NAME_YESTERDAY = '...';
// const S3_FLAG_FILE_KEY = '...';
// const S3_RESULT_FILE_KEY = '...';
// const RESULT_SHEET_COLUMN_COUNT = 8; // 例: A列からH列まで
// const ADD_COLOR = '#D9EAD3';
// const CHANGE_COLOR = '#FFF2CC';
// const DELETE_COLOR = '#F4CCCC';


// =============================================================================
// --- メイン処理 ---
// =============================================================================

/**
 * S3から取得したリンクチェック結果を処理するメイン関数です。
 * @param {object} e - トリガーイベントオブジェクト
 */
function mainPostProcess(e) {
  const isTriggeredExecution = (e !== undefined);

  if (isTriggeredExecution) {
    Logger.log("トリガーによる自動実行を検出しました。");
    const statusCheck = checkLambdaCompletionStatus_();
    if (!statusCheck.success) {
      Logger.log(`前提となるLambda処理が本日完了していないため、後続処理を中断しました。理由: ${statusCheck.reason}`);
      notifyPreconditionFailure_(statusCheck.reason);
      return;
    }
    Logger.log("Lambdaの本日分の正常完了を確認しました。後続処理を続行します。");
  } else {
    Logger.log("手動実行を検出しました。S3フラグファイルのチェックをスキップします。");
  }

  try {
    const spreadsheet = SpreadsheetApp.openById(CONFIG.SPREADSHEET_ID_WORK);
    let sheetToday = getOrCreateSheet_(spreadsheet, SHEET_NAME_TODAY);
    let sheetYesterday = getOrCreateSheet_(spreadsheet, SHEET_NAME_YESTERDAY);

    removeOldDeletedRows_(sheetToday);

    clearSheetContent_(sheetYesterday);
    backupPreviousResult_(sheetToday, sheetYesterday);
    clearSheetContent_(sheetToday);

    const csvText = fetchCsvFromS3_();
    writeCsvToSpreadsheet_(sheetToday, csvText);
    const diffDetails = compareAndHighlightDifferences_(sheetToday, sheetYesterday);

    sendCompletionEmail_(spreadsheet.getUrl(), diffDetails);

    if (isTriggeredExecution) {
      deleteS3FlagFile_();
    } else {
      Logger.log("手動実行のため、S3フラグファイルの削除をスキップします。");
    }

    Logger.log('全ての処理が正常に完了しました。');

  } catch (e) {
    handleError_(e);
  }
}

// =============================================================================
// --- ヘルパー関数 ---
// =============================================================================

/**
 * 指定された名前のシートを取得または作成します。
 * @param {Spreadsheet} spreadsheet - スプレッドシートオブジェクト
 * @param {string} sheetName - シート名
 * @returns {Sheet} シートオブジェクト
 * @private
 */
function getOrCreateSheet_(spreadsheet, sheetName) {
  let sheet = spreadsheet.getSheetByName(sheetName);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(sheetName);
    Logger.log(`シート「${sheetName}」を新規作成しました。`);
  }
  return sheet;
}

/**
 * Lambdaの処理が正常に完了したことを示すフラグファイルを確認します。
 * @returns {{success: boolean, reason: (string|undefined)}} 成功可否と、失敗時はその理由
 * @private
 */
function checkLambdaCompletionStatus_() {
  try {
    const s3 = S3.getInstance(CONFIG.AWS_ACCESS_KEY_ID, CONFIG.AWS_SECRET_ACCESS_KEY, CONFIG.S3_BUCKET_REGION);
    // Lambda側がContentType='application/json'でアップロードしているため、
    // S3ライブラリのgetObject()は自動的にJSON.parse済みのオブジェクトを返す（Blobではない）。
    // ファイルが存在しない場合はnullが返る。
    const statusData = s3.getObject(CONFIG.S3_BUCKET_NAME, S3_FLAG_FILE_KEY);
    if (!statusData) {
      const reason = `フラグファイル (s3://${CONFIG.S3_BUCKET_NAME}/${S3_FLAG_FILE_KEY}) が存在しません。Lambda処理が未実行、または処理中の可能性があります。`;
      Logger.log(reason);
      return { success: false, reason };
    }
    const today = Utilities.formatDate(new Date(), "Asia/Tokyo", "yyyy-MM-dd");

    if (statusData.status === 'SUCCESS' && statusData.last_success_date === today) {
      return { success: true };
    }

    const reason = statusData.status === 'FAILURE'
      ? `Lambda処理がエラー終了しました。エラー内容: ${statusData.error_message || '(不明)'}`
      : `Lambdaの完了ステータスが期待値と異なります。Status: ${statusData.status}, LastSuccessDate: ${statusData.last_success_date}`;
    Logger.log(reason);
    return { success: false, reason };
  } catch (e) {
    const reason = `Lambda完了ステータスファイルの確認処理でエラーが発生しました。Error: ${e.message}`;
    Logger.log(reason);
    return { success: false, reason };
  }
}

/**
 * Lambda処理の完了確認に失敗した（想定外の状態）ことを管理者に通知します。
 * @param {string} reason - 失敗理由
 * @private
 */
function notifyPreconditionFailure_(reason) {
  if (!CONFIG.EMAIL_ADDRESSES) {
    Logger.log('通知先メールアドレスが設定されていないため、メール送信をスキップしました。');
    return;
  }
  const subject = "【エラー】リンクチェック後処理 - Lambda処理の完了が未確認です";
  const body = `リンクチェック後処理（GAS）の実行時、Lambda処理の完了を確認できなかったため、後続処理を中断しました。

理由:
${reason}

Lambdaの実行状況、およびS3上のフラグファイル (${S3_FLAG_FILE_KEY}) をご確認ください。`;
  MailApp.sendEmail(CONFIG.EMAIL_ADDRESSES, subject, body);
  Logger.log('前提条件未達の通知メールを送信しました。');
}

/**
 * S3上のフラグファイルを削除します。
 * @private
 */
function deleteS3FlagFile_() {
  try {
    Logger.log(`処理完了のため、S3上のフラグファイル (${S3_FLAG_FILE_KEY}) を削除します...`);
    const s3 = S3.getInstance(CONFIG.AWS_ACCESS_KEY_ID, CONFIG.AWS_SECRET_ACCESS_KEY, CONFIG.S3_BUCKET_REGION);
    s3.deleteObject(CONFIG.S3_BUCKET_NAME, S3_FLAG_FILE_KEY);
    Logger.log('フラグファイルの削除が完了しました。');
  } catch (e) {
    const reason = `S3フラグファイル (${S3_FLAG_FILE_KEY}) の削除に失敗しました。Error: ${e.message}`;
    Logger.log(`警告: ${reason}`);
    notifyFlagFileDeletionFailure_(reason);
  }
}

/**
 * S3フラグファイルの削除に失敗したことを管理者に通知します。
 * 本体処理（結果反映・完了メール送信）自体は成功しているため、警告として通知します。
 * @param {string} reason - 失敗理由
 * @private
 */
function notifyFlagFileDeletionFailure_(reason) {
  if (!CONFIG.EMAIL_ADDRESSES) {
    Logger.log('通知先メールアドレスが設定されていないため、メール送信をスキップしました。');
    return;
  }
  const subject = "【警告】リンクチェック後処理 - フラグファイルの削除に失敗しました";
  const body = `リンクチェック後処理（GAS）自体は正常に完了しましたが、S3上のフラグファイルの削除に失敗しました。

理由:
${reason}

フラグファイルが残存していても翌日の判定（本日日付との突合）には影響しませんが、原因調査のためS3の状態をご確認ください。`;
  MailApp.sendEmail(CONFIG.EMAIL_ADDRESSES, subject, body);
  Logger.log('フラグファイル削除失敗の通知メールを送信しました。');
}

/**
 * S3からリンクチェック結果のCSVファイルを取得します。
 * @returns {string} CSVテキスト
 * @private
 */
function fetchCsvFromS3_() {
  const s3 = S3.getInstance(CONFIG.AWS_ACCESS_KEY_ID, CONFIG.AWS_SECRET_ACCESS_KEY, CONFIG.S3_BUCKET_REGION);
  const blob = s3.getObject(CONFIG.S3_BUCKET_NAME, S3_RESULT_FILE_KEY);
  if (blob) {
    return blob.getDataAsString('utf-8');
  }
  throw new Error(`S3からのファイル取得に失敗しました。Bucket: ${CONFIG.S3_BUCKET_NAME}, Key: ${S3_RESULT_FILE_KEY}`);
}

/**
 * シートから古い削除行（DELETE_COLORでマークされた行）を削除します。
 * @param {Sheet} sheet - 対象のシートオブジェクト
 * @private
 */
function removeOldDeletedRows_(sheet) {
  Logger.log(`シート「${sheet.getName()}」の古い削除行をクリーンアップします...`);
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return;

  const backgrounds = sheet.getRange(2, 1, lastRow - 1, 1).getBackgrounds();
  let rowsToDelete = 0;
  for (let i = backgrounds.length - 1; i >= 0; i--) {
    if (backgrounds[i][0].toUpperCase() === DELETE_COLOR.toUpperCase()) {
      rowsToDelete++;
    } else {
      break;
    }
  }
  if (rowsToDelete > 0) {
    const deleteStartRow = lastRow - rowsToDelete + 1;
    sheet.deleteRows(deleteStartRow, rowsToDelete);
    Logger.log(`${rowsToDelete}行の古い削除行を削除しました。`);
  }
}

/**
 * シートのデータ部分（2行目以降）をクリアします。
 * @param {Sheet} sheet - 対象のシートオブジェクト
 * @private
 */
function clearSheetContent_(sheet) {
  Logger.log(`「${sheet.getName()}」のデータ部分（2行目以降）をクリアします...`);
  if (sheet.getMaxRows() >= 2) {
    sheet.getRange(2, 1, sheet.getMaxRows() - 1, RESULT_SHEET_COLUMN_COUNT).clearContent().setBackground(null).setFontColor(null).setFontLine('none');
  }
}

/**
 * 前日の結果をバックアップします。
 * @param {Sheet} sheetToday - 当日結果シート
 * @param {Sheet} sheetYesterday - 前日結果シート
 * @private
 */
function backupPreviousResult_(sheetToday, sheetYesterday) {
  Logger.log(`前回結果をバックアップします (${sheetToday.getName()} -> ${sheetYesterday.getName()})...`);
  if (sheetToday.getLastRow() >= 2) {
    sheetToday.getRange(2, 1, sheetToday.getLastRow() - 1, RESULT_SHEET_COLUMN_COUNT).copyTo(sheetYesterday.getRange(2, 1), { contentsOnly: true });
  }
}

/**
 * CSVテキストをスプレッドシートに書き込みます。
 * @param {Sheet} sheet - 書き込み先のシート
 * @param {string} csvText - CSV形式のテキスト
 * @private
 */
function writeCsvToSpreadsheet_(sheet, csvText) {
  Logger.log(`シート「${sheet.getName()}」へCSVデータを書き込みます...`);
  if (!csvText || csvText.trim().length === 0) {
    Logger.log('CSVデータが空のため、書き込みをスキップしました。');
    return;
  }
  const data = Utilities.parseCsv(csvText);
  if (data.length <= 1) {
     Logger.log('CSVにデータ行がありませんでした。');
     return;
  }
  const bodyValues = data.slice(1);
  sheet.getRange(2, 1, bodyValues.length, bodyValues[0].length).setValues(bodyValues);
  Logger.log(`${bodyValues.length}行のデータを書き込みました。`);
}

/**
 * 当日と前日のシートを比較し、差分をハイライトします。
 * @param {Sheet} sheetToday - 当日結果シート
 * @param {Sheet} sheetYesterday - 前日結果シート
 * @returns {string} メール本文用の差分詳細テキスト
 * @private
 */
function compareAndHighlightDifferences_(sheetToday, sheetYesterday) {
  const STATUS_CODE_COLUMN_INDEX = 3; // 確認結果列 (D列)
  const TIMESTAMP_COLUMN_INDEX = 7;   // タイムスタンプ列 (H列)
  const START_ROW = 2;

  const todayValues = sheetToday.getLastRow() >= START_ROW ? sheetToday.getRange(START_ROW, 1, sheetToday.getLastRow() - START_ROW + 1, RESULT_SHEET_COLUMN_COUNT).getValues() : [];
  const yesterdayValues = sheetYesterday.getLastRow() >= START_ROW ? sheetYesterday.getRange(START_ROW, 1, sheetYesterday.getLastRow() - START_ROW + 1, RESULT_SHEET_COLUMN_COUNT).getValues() : [];

  if (todayValues.length === 0 && yesterdayValues.length === 0) {
    return "比較対象のデータがありませんでした。";
  }

  const yesterdayMap = new Map(yesterdayValues.map(row => [`${row[1]}|${row[2]}`, row]));
  const results = { added: [], changed: [], deleted: [] };
  const backgrounds = todayValues.map(() => Array(RESULT_SHEET_COLUMN_COUNT).fill(null));
  const fontColors = todayValues.map(() => Array(RESULT_SHEET_COLUMN_COUNT).fill('black'));
  const fontLines = todayValues.map(() => Array(RESULT_SHEET_COLUMN_COUNT).fill('none'));

  todayValues.forEach((todayRow, rowIndex) => {
    const key = `${todayRow[1]}|${todayRow[2]}`;
    if (yesterdayMap.has(key)) {
      const yesterdayRow = yesterdayMap.get(key);
      let isRowDifferent = false;
      let changes = [];
      for (let colIndex = 0; colIndex < RESULT_SHEET_COLUMN_COUNT; colIndex++) {
        if (colIndex === TIMESTAMP_COLUMN_INDEX) continue;

        if (String(todayRow[colIndex]).trim() !== String(yesterdayRow[colIndex]).trim()) {
          isRowDifferent = true;
          fontColors[rowIndex][colIndex] = 'red';
          changes.push(`  - ${sheetToday.getRange(1, colIndex + 1).getValue()}: 「${yesterdayRow[colIndex]}」->「${todayRow[colIndex]}」`);
        }
      }
      if (isRowDifferent) {
        backgrounds[rowIndex].fill(CHANGE_COLOR);
        // ★修正: キーを statusCode から checkResult に変更
        results.changed.push({ pageUrl: todayRow[1], link: todayRow[2], checkResult: todayRow[STATUS_CODE_COLUMN_INDEX], details: changes.join('\n') });
      }
      yesterdayMap.delete(key);
    } else {
      backgrounds[rowIndex].fill(ADD_COLOR);
      fontColors[rowIndex].fill('red');
      // ★修正: キーを statusCode から checkResult に変更
      results.added.push({ pageUrl: todayRow[1], link: todayRow[2], checkResult: todayRow[STATUS_CODE_COLUMN_INDEX] });
    }
  });

  yesterdayMap.forEach(deletedRow => {
    // ★修正: キーを statusCode から checkResult に変更
    results.deleted.push({ pageUrl: deletedRow[1], link: deletedRow[2], checkResult: deletedRow[STATUS_CODE_COLUMN_INDEX], data: deletedRow });
  });

  if (todayValues.length > 0) {
    sheetToday.getRange(START_ROW, 1, todayValues.length, RESULT_SHEET_COLUMN_COUNT).setBackgrounds(backgrounds).setFontColors(fontColors).setFontLines(fontLines);
  }
  if (results.deleted.length > 0) {
    const appendStartRow = sheetToday.getLastRow() + 1;
    const deletedData = results.deleted.map(item => item.data);
    sheetToday.getRange(appendStartRow, 1, deletedData.length, RESULT_SHEET_COLUMN_COUNT).setValues(deletedData).setBackground(DELETE_COLOR).setFontLine('line-through');
  }
  return formatDiffEmailBody_(results);
}

/**
 * 差分結果をメール本文用にフォーマットします。
 * @param {object} results - 差分結果オブジェクト
 * @returns {string} メール本文
 * @private
 */
function formatDiffEmailBody_(results) {
  if (results.added.length === 0 && results.changed.length === 0 && results.deleted.length === 0) {
    return "前回チェック時からの差分はありませんでした。";
  }
  let body = "前回チェック時から以下の差分が検出されました。";

  // ★修正: item.statusCode を item.checkResult に変更し、表示名は「確認結果」のままにする
  const formatItem = item => `  - 記事URL: ${item.pageUrl}\n    広告URL: ${item.link}\n    確認結果: ${item.checkResult}`;

  if (results.added.length > 0) {
    body += `\n▼ 追加 (${results.added.length}件)\n`;
    body += results.added.map(formatItem).join('\n\n');
  }
  if (results.changed.length > 0) {
    body += `\n\n▼ 変更 (${results.changed.length}件)\n`;
    // ★修正: 共通情報と変更詳細の間に改行と「■変更」を挿入
    body += results.changed.map(item => `${formatItem(item)}\n\n  ■ 変更内容\n${item.details}`).join('\n\n');
  }
  if (results.deleted.length > 0) {
    body += `\n\n▼ 削除 (${results.deleted.length}件)\n`;
    body += results.deleted.map(formatItem).join('\n\n');
  }
  return body;
}

/**
 * 処理完了をメールで通知します。
 * @param {string} spreadsheetUrl - スプレッドシートのURL
 * @param {string} diffDetails - 差分詳細
 * @private
 */
function sendCompletionEmail_(spreadsheetUrl, diffDetails) {
  if (!CONFIG.EMAIL_ADDRESSES) {
    Logger.log('通知先メールアドレスが設定されていないため、メール送信をスキップしました。');
    return;
  }
  const subject = "リンクチェック処理完了通知";
  const body = `リンクチェック処理が完了しました。

${diffDetails}

詳細は以下のスプレッドシートを確認してください。
${spreadsheetUrl}`;
  MailApp.sendEmail(CONFIG.EMAIL_ADDRESSES, subject, body);
  Logger.log('完了通知メールを送信しました。');
}

/**
 * エラー処理と通知を行います。
 * @param {Error} e - 発生したエラーオブジェクト
 * @private
 */
function handleError_(e) {
  Logger.log(`エラーが発生しました: ${e.toString()}`);
  Logger.log(`Stack Trace: ${e.stack}`);
  if (CONFIG.EMAIL_ADDRESSES) {
    const subject = "【エラー】リンクチェック後処理";
    const body = `処理中にエラーが発生しました。

エラー内容:
${e.toString()}

ログを確認してください。`;
    MailApp.sendEmail(CONFIG.EMAIL_ADDRESSES, subject, body);
  }
}