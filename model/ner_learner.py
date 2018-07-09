from fastai.text import *
from .data_utils import pad_sequences, minibatches, get_chunks
from .crf import CRF
from .general_utils import Progbar
from torch.optim.lr_scheduler import StepLR


class NERLearner(object):
    """
    NERLearner class that encapsulates a pytorch nn.Module model and ModelData class
    Contains methods for training a testing the model
    """
    def __init__(self, config, model):
        super().__init__()
        self.config = config
        self.logger = self.config.logger
        self.model = model
        self.model_path = config.dir_model
        self.use_cuda = False
        if torch.cuda.is_available():
            self.use_cuda = True
            self.logger.info("GPU found.")
            self.model = model.cuda()
        else:
            self.logger.info("No GPU found.")
        self.idx_to_tag = {idx: tag for tag, idx in
                           self.config.vocab_tags.items()}
        self.criterion = CRF(self.config.ntags).cuda()
        self.optimizer = optim.Adam(self.model.parameters())

    def get_model_path(self, name):
        return os.path.join(self.model_path,name)+'.h5'

    def get_layer_groups(self, do_fc=False):
        return children(self.model)

    def freeze_to(self, n):
        c=self.get_layer_groups()
        for l in c:
            set_trainable(l, False)
        for l in c[n:]:
            set_trainable(l, True)

    def unfreeze(self):
        self.freeze_to(0)

    def save(self, name=None):
        if not name:
            name = self.config.ner_model_path
        save_model(self.model, self.get_model_path(name))
        self.logger.info(f"Saved model at {self.get_model_path(name)}")

    def load_emb(self):
        self.model.emb.weight = nn.Parameter(T(self.config.embeddings))
        self.model.emb.weight.requires_grad = False
        self.logger.info('Loading pretrained word embeddings')

    def load(self, fn=None):
        if not fn: fn = self.config.ner_model_path
        fn = self.get_model_path(fn)
        load_ner_model(self.model, fn, strict=True)
        self.logger.info(f"Loaded model from {fn}")

    def batch_iter(self, train, batch_size, return_lengths=False, shuffle=False, sorter=False):
        """
        Builds a generator from the given dataloader to be fed into the model

        Args:
            train: DataLoader
            batch_size: size of each batch
            return_lengths: if True, generator returns a list of sequence lengths for each
                            sample in the batch
                            ie. sequence_lengths = [8,7,4,3]
            shuffle: if True, shuffles the data for each epoch
            sorter: if True, uses a sorter to shuffle the data

        Returns:
            nbatches: (int) number of batches
            data_generator: batch generator yielding
                                dict inputs:{'word_ids' : np.array([[padded word_ids in sent1], ...])
                                             'char_ids': np.array([[[padded char_ids in word1_sent1], ...],
                                                                    [padded char_ids in word1_sent2], ...],
                                                                    ...])}
                                labels: np.array([[padded label_ids in sent1], ...])
                                sequence_lengths: list([len(sent1), len(sent2), ...])

        """
        nbatches = (len(train) + batch_size - 1) // batch_size

        def data_generator():
            while True:
                if shuffle: train.shuffle()
                elif sorter==True and train.sorter: train.sort()

                for i, (words, labels) in enumerate(minibatches(train, batch_size)):

                    # perform padding of the given data
                    if self.config.use_chars:
                        char_ids, word_ids = zip(*words)
                        word_ids, sequence_lengths = pad_sequences(word_ids, 1)
                        char_ids, word_lengths = pad_sequences(char_ids, pad_tok=0,
                        nlevels=2)

                    else:
                        char_ids, word_ids = zip(*words)
                        word_ids, sequence_lengths = pad_sequences(word_ids, 0)

                    if labels:
                        labels, _ = pad_sequences(labels, 0)
                        # if categorical
                        ## labels = [to_categorical(label, num_classes=len(train.tag_itos)) for label in labels]

                    # build dictionary
                    inputs = {
                        "word_ids": np.asarray(word_ids)
                    }

                    if self.config.use_chars:
                        inputs["char_ids"] = np.asarray(char_ids)

                    if return_lengths:
                        yield(inputs, np.asarray(labels), sequence_lengths)

                    else:
                        yield (inputs, np.asarray(labels))

        return (nbatches, data_generator())


    def fine_tune(self):
        """
        Fine tune the NER model by freezing the pre-trained encoder and training the newly
        instantiated layers for 1 epochs
        """
        self.logger.info("Fine Tuning Model")
        self.fit(epochs=1, fine_tune=True)


    def fit(self, train, dev, epochs=None, fine_tune=False):
        """
        Fits the model to the training dataset and evaluates on the validation set.
        Saves the model to disk
        """
        if not epochs:
            epochs = self.config.nepochs
        batch_size = self.config.batch_size

        nbatches_train, train_generator = self.batch_iter(train, batch_size,
                                                          return_lengths=True)
        nbatches_dev, dev_generator =     self.batch_iter(dev, batch_size,
                                                          return_lengths=True)

        scheduler = StepLR(self.optimizer, step_size=1, gamma=self.config.lr_decay)

        if not fine_tune: self.logger.info("Training Model")
        for epoch in range(epochs):
            scheduler.step()
            self.train(epoch, nbatches_train, train_generator, fine_tune=fine_tune)
            self.test(nbatches_dev, dev_generator, fine_tune=fine_tune)

        if fine_tune:
            self.save(self.config.ner_ft_path)
        else :
            self.save(self.config.ner_model_path)


    def train(self, epoch, nbatches_train, train_generator, fine_tune=False):
        self.logger.info('\nEpoch: %d' % epoch)
        self.model.train()
        self.model.emb.weight.requires_grad = False

        train_loss = 0
        correct = 0
        total = 0

        prog = Progbar(target=nbatches_train)

        for batch_idx, (inputs, targets, sequence_lengths) in enumerate(train_generator):

            if batch_idx == nbatches_train: break
            if inputs['word_ids'].shape[0] == 1:
                self.logger.info('Skipping batch of size=1')
                continue

            word_input = T(inputs['word_ids'], cuda=self.use_cuda)
            char_input = T(inputs['char_ids'], cuda=self.use_cuda)
            targets = T(targets, cuda=self.use_cuda).transpose(0,1).contiguous()
            self.optimizer.zero_grad()

            word_input, char_input, targets = Variable(word_input, requires_grad=False), \
                                              Variable(char_input, requires_grad=False),\
                                              Variable(targets)

            inputs = (word_input, char_input)
            outputs = self.model(inputs)

            # Create mask
            mask = create_mask(sequence_lengths, targets)
            # Get CRF Loss
            loss = -1*self.criterion(outputs, targets, mask=mask)
            loss.backward()
            self.optimizer.step()

            # Callbacks
            train_loss += loss.data[0] #loss.item()
            predictions = self.criterion.decode(outputs, mask=mask)
            masked_targets = mask_targets(targets, sequence_lengths)

            t_ = mask.type(torch.LongTensor).sum().data[0]
            total += t_
            c_ = sum([p[i] == mt[i] for p, mt in zip(predictions, masked_targets) for i in range(len(p))])
            correct += c_

            prog.update(batch_idx + 1, values=[("train loss", loss.data[0])], exact=[("Accuracy", 100*c_/t_)])

        self.logger.info("Train Accuracy: %.3f%% (%d/%d)" %(100.*correct/total, correct, total) )


    def test(self, nbatches_val, val_generator, fine_tune=False):
        self.model.eval()
        accs = []
        test_loss = 0
        correct_preds = 0
        total_correct = 0
        total_preds = 0
        total_step = None

        for batch_idx, (inputs, targets, sequence_lengths) in enumerate(val_generator):
            if batch_idx == nbatches_val: break
            if inputs['word_ids'].shape[0] == 1:
                self.logger.info('Skipping batch of size=1')
                continue

            total_step = batch_idx
            word_input = T(inputs['word_ids'], cuda=self.use_cuda)
            char_input = T(inputs['char_ids'], cuda=self.use_cuda)
            targets = T(targets, cuda=self.use_cuda).transpose(0,1).contiguous()

            word_input, char_input, targets = Variable(word_input, requires_grad=False), \
                                              Variable(char_input, requires_grad=False),\
                                              Variable(targets)

            inputs = (word_input, char_input)
            outputs = self.model(inputs)

            # Create mask
            mask = create_mask(sequence_lengths, targets)

            # Get CRF Loss
            loss = self.criterion(outputs, targets, mask=mask)
            loss *= -1

            # Callbacks
            test_loss += loss.data[0] #loss.item()
            predictions = self.criterion.decode(outputs, mask=mask)
            masked_targets = mask_targets(targets, sequence_lengths)

            for lab, lab_pred in zip(masked_targets, predictions):

                accs    += [a==b for (a, b) in zip(lab, lab_pred)]

                lab_chunks      = set(get_chunks(lab, self.config.vocab_tags))
                lab_pred_chunks = set(get_chunks(lab_pred,
                                                 self.config.vocab_tags))

                correct_preds += len(lab_chunks & lab_pred_chunks)
                total_preds   += len(lab_pred_chunks)
                total_correct += len(lab_chunks)

        p   = correct_preds / total_preds if correct_preds > 0 else 0
        r   = correct_preds / total_correct if correct_preds > 0 else 0
        f1  = 2 * p * r / (p + r) if correct_preds > 0 else 0
        acc = np.mean(accs)

        self.logger.info("Val Loss : %.3f, Val Accuracy: %.3f%%, Val F1: %.3f%%" %(test_loss/(total_step+1), 100*acc, 100*f1))


    def evaluate(self,test):
        batch_size = self.config.batch_size
        nbatches_test, test_generator = self.batch_iter(test, batch_size,
                                                        return_lengths=True)
        self.logger.info('Evaluating on test set')
        self.test(nbatches_test, test_generator)

    def predict_batch(self, words):
        self.model.eval()
        mult = np.ones(self.config.batch_size).reshape(self.config.batch_size, 1).astype(int)

        char_ids, word_ids = zip(*words)
        word_ids, sequence_lengths = pad_sequences(word_ids, 1)
        char_ids, word_lengths = pad_sequences(char_ids, pad_tok=0,
                                               nlevels=2)
        word_ids = np.asarray(word_ids)
        char_ids = np.asarray(char_ids)

        word_input = T(mult*word_ids, cuda=self.use_cuda)
        char_input = T((mult*char_ids.transpose(1,0,2)).transpose(1,0,2), cuda=self.use_cuda)

        word_input, char_input = Variable(word_input, requires_grad=False), \
                                 Variable(char_input, requires_grad=False)



        inputs = (word_input, char_input)
        outputs = self.model(inputs)

        predictions = self.criterion.decode(outputs)

        return predictions[0]

    def predict(self, words_raw):
        """Returns list of tags

        Args:
            words_raw: list of words (string), just one sentence (no batch)

        Returns:
            preds: list of tags (string), one for each word in the sentence

        """
        words = [self.config.processing_word(w) for w in words_raw]
        if type(words[0]) == tuple:
            words = zip(*words)
        pred_ids = self.predict_batch([words])
        preds = [self.idx_to_tag[idx] for idx in pred_ids]

        return preds


def create_mask(sequence_lengths, targets, batch_first=False):
    """ Creates binary mask """
    mask = Variable(torch.ones(targets.size()).type(torch.ByteTensor)).cuda()

    for i,l in enumerate(sequence_lengths):
        if batch_first:
            if l < targets.size(1):
                mask.data[i, l:] = 0
        else:
            if l < targets.size(0):
                mask.data[l:, i] = 0

    return mask


def mask_targets(targets, sequence_lengths, batch_first=False):
    """ Masks the targets """
    if not batch_first:
         targets = targets.transpose(0,1)
    t = []
    for l, p in zip(targets,sequence_lengths):
        t.append(l[:p].data.tolist())
    return t


def load_ner_model(m, p, strict=True):
    sd = torch.load(p, map_location=lambda storage, loc: storage)
    names = set(m.state_dict().keys())
    for n in list(sd.keys()): # list "detatches" the iterator
        if n not in names and n+'_raw' in names:
            if n+'_raw' not in sd: sd[n+'_raw'] = sd[n]
            del sd[n]
    m.load_state_dict(sd, strict=strict)
