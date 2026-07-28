function export_trialinfo(preProcessedPath, metaDataExtFilePath, outputDir, loadColumnName)
% EXPORT_TRIALINFO Export per-trial condition labels for the warehouse.
%
% Why this exists
% ---------------
% The cycle-feature CSVs written by RunBycycle.py contain trial, channel_idx and
% channel_label but no condition column: 'trial' is only a positional index. The
% condition (memory load) lives in subjectData.trialinfo, produced by
% defineTrialsStCat during processSubjects, with column meanings held separately
% in metaData.trialInfoLabels.
%
% Without this export the warehouse can build the fact table but cannot form the
% load 1 vs load 3 pairs the analysis needs, so the trial_condition_coverage
% quality check fails and the run stops.
%
% Syntax
%   export_trialinfo(preProcessedPath, metaDataExtFilePath, outputDir)
%   export_trialinfo(preProcessedPath, metaDataExtFilePath, outputDir, 'setSize')
%
% Inputs
%   preProcessedPath    - directory holding *_allChanSpkRmvl_trialInfo.mat
%                         (metaDataExt.projectPaths.preProcessedPath)
%   metaDataExtFilePath - path to metaDataExt.mat, for trialInfoLabels
%   outputDir           - destination for <subject>_trial_metadata.csv
%   loadColumnName      - (optional) label identifying the load column in
%                         trialInfoLabels. Defaults to a search over
%                         {'load','setSize','set_size','nItems','memoryLoad'}.
%
% Output CSV contract (one file per subject, consumed by ingest.load_trial_metadata)
%   subject_id, trial, load_condition, correct
%
% Note on trial indexing
%   RunBycycle.py enumerates trials with a 0-based Python loop and writes that
%   index. MATLAB rows are 1-based. This function subtracts 1 so the exported
%   trial numbers join directly against the feature CSVs. Getting this wrong
%   produces an off-by-one that silently mislabels every trial's condition, so
%   the join coverage is checked in the warehouse rather than assumed here.

    arguments
        preProcessedPath    (1,:) char
        metaDataExtFilePath (1,:) char
        outputDir           (1,:) char
        loadColumnName      (1,:) char = ''
    end

    if ~isfolder(outputDir)
        mkdir(outputDir);
    end

    meta = load(metaDataExtFilePath);
    if ~isfield(meta, 'metaDataExt')
        error('export_trialinfo:missingMetaData', ...
              'metaDataExt not found in %s', metaDataExtFilePath);
    end
    metaDataExt = meta.metaDataExt;

    labels = cellstr(string(metaDataExt.trialInfoLabels));
    loadCol = resolveColumn(labels, loadColumnName, ...
        {'load', 'setSize', 'set_size', 'nItems', 'memoryLoad'});
    correctCol = resolveColumn(labels, '', {'correct', 'accuracy', 'isCorrect'});

    fprintf('Using trialinfo column %d (%s) as load_condition.\n', ...
            loadCol, labels{loadCol});
    if isnan(correctCol)
        fprintf('No accuracy column found; correct will be written empty.\n');
    end

    files = dir(fullfile(preProcessedPath, '*_allChanSpkRmvl_trialInfo.mat'));
    if isempty(files)
        error('export_trialinfo:noInputs', ...
              'No *_allChanSpkRmvl_trialInfo.mat files under %s', preProcessedPath);
    end

    for iFile = 1:numel(files)
        inputPath = fullfile(files(iFile).folder, files(iFile).name);
        subjectID = extractBefore(files(iFile).name, '_allChanSpkRmvl_trialInfo.mat');

        loaded = load(inputPath, 'trialinfo');
        if ~isfield(loaded, 'trialinfo')
            warning('export_trialinfo:noTrialInfo', ...
                    'trialinfo absent in %s; skipping.', files(iFile).name);
            continue;
        end
        trialinfo = loaded.trialinfo;

        if size(trialinfo, 2) < loadCol
            warning('export_trialinfo:narrowTrialInfo', ...
                    '%s has %d trialinfo columns, load column is %d; skipping.', ...
                    subjectID, size(trialinfo, 2), loadCol);
            continue;
        end

        nTrials = size(trialinfo, 1);
        % 0-based to match the Python trial index written by RunBycycle.py.
        trialIndex = (0:nTrials - 1)';
        loadCondition = trialinfo(:, loadCol);

        if isnan(correctCol) || size(trialinfo, 2) < correctCol
            correctFlag = strings(nTrials, 1);
        else
            correctFlag = string(logical(trialinfo(:, correctCol)));
        end

        outputPath = fullfile(outputDir, sprintf('%s_trial_metadata.csv', subjectID));
        fid = fopen(outputPath, 'w');
        if fid == -1
            warning('export_trialinfo:cannotWrite', ...
                    'Cannot open %s for writing; skipping.', outputPath);
            continue;
        end
        fprintf(fid, 'subject_id,trial,load_condition,correct\n');
        for iTrial = 1:nTrials
            fprintf(fid, '%s,%d,%d,%s\n', ...
                    subjectID, trialIndex(iTrial), ...
                    loadCondition(iTrial), correctFlag(iTrial));
        end
        fclose(fid);

        fprintf('Wrote %d trials for %s to %s\n', nTrials, subjectID, outputPath);
    end
end

function idx = resolveColumn(labels, requested, candidates)
% Resolve a trialinfo column index by label, case-insensitively.
    if ~isempty(requested)
        idx = find(strcmpi(labels, requested), 1);
        if isempty(idx)
            error('export_trialinfo:columnNotFound', ...
                  'Requested column "%s" not in trialInfoLabels: %s', ...
                  requested, strjoin(labels, ', '));
        end
        return;
    end

    for iCandidate = 1:numel(candidates)
        idx = find(strcmpi(labels, candidates{iCandidate}), 1);
        if ~isempty(idx)
            return;
        end
    end

    if nargout > 0
        idx = NaN;
    end
end
